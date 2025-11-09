from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional

import boto3

from ind_pipeline.consumer import ModuleConfig, NotificationConsumer
from ind_pipeline.registry import ModuleDescriptor, register_module
from ind_pipeline.utils import utc_timestamp

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StageModuleSpec:
    module_name: str
    env_prefix: str
    description: str
    default_queue_name: str
    default_topic_env: str = "SNS_TOPIC_ARN"


class SimpleStageModule:
    """
    Convenience wrapper for modules that only need basic SNS/SQS plumbing.
    """

    def __init__(
        self,
        spec: StageModuleSpec,
        *,
        work_fn: Optional[Callable[[Dict[str, object]], Dict[str, object]]] = None,
        stage_key: Optional[str] = None,
        sns_client: Optional[Any] = None,
    ) -> None:
        self.spec = spec
        self._work_fn = work_fn
        self._stage_key = stage_key or spec.module_name
        self._sns = sns_client or boto3.client("sns")
        register_module(
            ModuleDescriptor(
                name=spec.module_name,
                description=spec.description,
                entrypoint=self.run,
            )
        )

    # ------------------------------------------------------------------
    def handler(self, payload: Dict[str, object]) -> Dict[str, object]:
        """
        Default handler that enriches the payload with stage metadata.
        Provides a hook via work_fn for module-specific processing.
        """
        work_payload: Dict[str, object] = {}
        if self._work_fn:
            try:
                work_payload = dict(self._work_fn(payload) or {})
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("[%s] work_fn failed: %s", self.spec.module_name, exc)
                work_payload = {"status": "failed", "error_message": str(exc)}
        result = dict(work_payload)
        result.setdefault("stage", self._stage_key)
        result.setdefault("status", "completed")
        result.setdefault("received_keys", sorted(payload.keys()))
        result.setdefault("completed_at", utc_timestamp())
        self._publish_next_events(payload, result)
        return result

    # ------------------------------------------------------------------
    def run(self) -> None:
        consumer = NotificationConsumer(self._build_config(), self.handler)
        consumer.run()

    # ------------------------------------------------------------------
    def _build_config(self) -> ModuleConfig:
        topic_arn = self._resolve_topic()
        queue_name = self._resolve_env("QUEUE_NAME", self.spec.default_queue_name)
        completion_topic = self._resolve_env("COMPLETION_TOPIC_ARN")
        wait_seconds = int(self._resolve_env("WAIT_TIME_SECONDS", "20"))
        poll_interval = int(self._resolve_env("POLL_INTERVAL", "5"))
        max_messages = int(self._resolve_env("MAX_MESSAGES", "10"))

        return ModuleConfig(
            name=self.spec.module_name,
            topic_arn=topic_arn,
            queue_name=queue_name,
            completion_topic_arn=completion_topic,
            wait_time_seconds=wait_seconds,
            poll_interval=poll_interval,
            max_messages=max_messages,
        )

    # ------------------------------------------------------------------
    def _resolve_topic(self) -> str:
        specific = self._resolve_env("TOPIC_ARN")
        if specific:
            return specific
        fallback_env = self.spec.default_topic_env
        fallback = os.getenv(fallback_env)
        if fallback:
            return fallback
        raise RuntimeError(
            f"Environment variable {self.spec.env_prefix}_TOPIC_ARN or {fallback_env} "
            f"is required for module {self.spec.module_name}"
        )

    # ------------------------------------------------------------------
    def _resolve_env(self, suffix: str, default: Optional[str] = None) -> Optional[str]:
        env_name = f"{self.spec.env_prefix}_{suffix}"
        value = os.getenv(env_name)
        if value is not None and value.strip():
            return value.strip()
        if default is not None:
            return default
        return None

    # ------------------------------------------------------------------
    def _publish_next_events(self, payload: Dict[str, object], result: Dict[str, object]) -> None:
        topics = list(self._iter_next_topics())
        if not topics:
            return

        message_payload = {
            "stage": self.spec.module_name,
            "status": result.get("status"),
            "completed_at": result.get("completed_at") or utc_timestamp(),
            "input": payload,
            "output": result,
        }
        message = json.dumps(message_payload)

        for topic in topics:
            try:
                self._sns.publish(TopicArn=topic, Message=message)
                logger.info("[%s] published downstream event to %s", self.spec.module_name, topic)
            except Exception as exc:  # pragma: no cover - network failure
                logger.warning(
                    "[%s] failed to publish next stage event to %s: %s",
                    self.spec.module_name,
                    topic,
                    exc,
                )

    def _iter_next_topics(self) -> Iterable[str]:
        topics: List[str] = []
        raw_list = self._resolve_env("NEXT_TOPIC_ARNS")
        if raw_list:
            topics.extend(item.strip() for item in raw_list.split(",") if item.strip())

        single_topic = self._resolve_env("NEXT_TOPIC_ARN")
        if single_topic:
            topics.append(single_topic.strip())

        seen: set[str] = set()
        for topic in topics:
            if topic and topic not in seen:
                seen.add(topic)
                yield topic
