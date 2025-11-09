"""
Reusable SNS/SQS consumer foundation for independent pipeline modules.

Each module provides a handler callable that receives a parsed message
payload (dictionary) and returns a dictionary result. The consumer takes
care of polling the queue, spinning up a worker thread per message, and
publishing an optional completion notification.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


MessageHandler = Callable[[Dict[str, Any]], Dict[str, Any]]
PayloadParser = Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]


@dataclass(slots=True)
class ModuleConfig:
    """
    Configuration describing how a module consumes and emits SNS messages.
    """

    name: str
    topic_arn: str
    queue_name: str
    completion_topic_arn: Optional[str] = None
    wait_time_seconds: int = 20
    poll_interval: int = 5
    max_messages: int = 10
    queue_attributes: Dict[str, str] = field(
        default_factory=lambda: {"ReceiveMessageWaitTimeSeconds": "20"}
    )


class NotificationConsumer:
    """
    Polls an SQS queue subscribed to an SNS topic and dispatches work
    to a handler using dedicated threads.
    """

    def __init__(
        self,
        config: ModuleConfig,
        handler: MessageHandler,
        *,
        parser: Optional[PayloadParser] = None,
        sqs_resource: Optional[Any] = None,
        sns_client: Optional[Any] = None,
    ) -> None:
        if not config.topic_arn:
            raise ValueError("ModuleConfig.topic_arn is required")
        if not config.queue_name:
            raise ValueError("ModuleConfig.queue_name is required")

        self.config = config
        self._handler = handler
        self._parser = parser or (lambda payload: payload)
        self._sqs = sqs_resource or boto3.resource("sqs")
        self._sns = sns_client or boto3.client("sns")
        self._queue_url: Optional[str] = None

    # ------------------------------------------------------------------
    def ensure_subscription(self) -> str:
        """
        Ensure the queue exists, has the correct policy, and is subscribed
        to the configured SNS topic. Returns the queue URL.
        """
        try:
            queue = self._sqs.create_queue(
                QueueName=self.config.queue_name,
                Attributes=self.config.queue_attributes,
            )
        except ClientError as exc:
            logger.error("[%s] failed to create SQS queue %s: %s", self.config.name, self.config.queue_name, exc)
            raise

        queue_url = queue.url
        queue_arn = queue.attributes["QueueArn"]

        # Subscribe the queue to the topic if not already subscribed.
        paginator = self._sns.get_paginator("list_subscriptions_by_topic")
        existing = False
        for page in paginator.paginate(TopicArn=self.config.topic_arn):
            for subscription in page.get("Subscriptions", []):
                if subscription.get("Endpoint") == queue_arn:
                    existing = True
                    break
            if existing:
                break

        if not existing:
            self._sns.subscribe(
                TopicArn=self.config.topic_arn,
                Protocol="sqs",
                Endpoint=queue_arn,
                Attributes={"RawMessageDelivery": "true"},
            )
            logger.info(
                "[%s] subscribed SQS queue %s to SNS topic %s",
                self.config.name,
                self.config.queue_name,
                self.config.topic_arn,
            )

        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "SNSSubscriptionPolicy",
                    "Effect": "Allow",
                    "Principal": {"Service": "sns.amazonaws.com"},
                    "Action": "sqs:SendMessage",
                    "Resource": queue_arn,
                    "Condition": {"ArnEquals": {"aws:SourceArn": self.config.topic_arn}},
                }
            ],
        }
        queue.set_attributes(Attributes={"Policy": json.dumps(policy)})

        self._queue_url = queue_url
        return queue_url

    # ------------------------------------------------------------------
    def _decode_message(self, message_body: str) -> Optional[Dict[str, Any]]:
        try:
            payload = json.loads(message_body)
        except json.JSONDecodeError:
            logger.warning("[%s] received non-JSON message body: %s", self.config.name, message_body)
            return None
        try:
            parsed = self._parser(payload) if self._parser else payload
        except Exception as exc:  # pragma: no cover - parser is user provided
            logger.warning("[%s] parser failed for payload %s: %s", self.config.name, payload, exc)
            return None
        return parsed

    # ------------------------------------------------------------------
    def _publish_completion(self, payload: Dict[str, Any]) -> None:
        if not self.config.completion_topic_arn:
            return
        try:
            self._sns.publish(
                TopicArn=self.config.completion_topic_arn,
                Message=json.dumps(payload),
            )
            logger.info(
                "[%s] published completion message to %s",
                self.config.name,
                self.config.completion_topic_arn,
            )
        except ClientError as exc:  # pragma: no cover - network failure
            logger.warning(
                "[%s] failed to publish completion message: %s",
                self.config.name,
                exc,
            )

    # ------------------------------------------------------------------
    def _run_handler(self, payload: Dict[str, Any]) -> None:
        module_name = self.config.name
        start = time.perf_counter()
        status = "succeeded"
        result: Dict[str, Any] = {}
        error_message: Optional[str] = None
        try:
            result = self._handler(payload) or {}
        except Exception as exc:  # pragma: no cover - handler defined by caller
            status = "failed"
            error_message = str(exc)
            logger.exception("[%s] handler failed for payload %s", module_name, payload)
        duration = time.perf_counter() - start

        completion_payload = {
            "module": module_name,
            "status": status,
            "duration_seconds": round(duration, 3),
            "request": payload,
            "result": result,
        }
        if error_message:
            completion_payload["error"] = {"message": error_message}

        self._publish_completion(completion_payload)

    # ------------------------------------------------------------------
    def _dispatch(self, payload: Dict[str, Any]) -> None:
        thread = threading.Thread(
            target=self._run_handler,
            args=(payload,),
            daemon=True,
        )
        thread.start()

    # ------------------------------------------------------------------
    def worker_loop(self) -> None:
        if not self._queue_url:
            self.ensure_subscription()

        assert self._queue_url is not None  # satisfied by ensure_subscription
        queue = self._sqs.Queue(self._queue_url)

        logger.info("[%s] listening for messages on %s", self.config.name, self._queue_url)
        while True:
            messages = queue.receive_messages(
                MaxNumberOfMessages=self.config.max_messages,
                WaitTimeSeconds=self.config.wait_time_seconds,
            )
            if not messages:
                if self.config.poll_interval:
                    time.sleep(self.config.poll_interval)
                continue

            for message in messages:
                payload = self._decode_message(message.body)
                if payload:
                    self._dispatch(payload)
                message.delete()

    # ------------------------------------------------------------------
    def run(self) -> None:
        """
        Convenience helper to ensure the subscription is ready and start the
        infinite worker loop.
        """
        self.ensure_subscription()
        self.worker_loop()
