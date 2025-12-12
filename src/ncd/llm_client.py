from typing import Any, Dict

from pydantic import BaseModel

from .config import settings

# You can wire this to OpenAI, local Llama, etc.
# Here it's just an interface; you implement the actual call.


class LLMClient:
    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or settings.llm_model_name

    def extract_json(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: BaseModel | None = None,
    ) -> Dict[str, Any]:
        """
        Call your LLM and parse JSON response.
        For now this is a stub; you insert your actual API call.
        """
        # TODO: implement with your provider
        # You should:
        #   1. send system+user prompt
        #   2. enforce JSON output
        #   3. parse JSON into Python dict
        raise NotImplementedError("Wire this to your LLM provider")

    def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        """
        Call your LLM for free-form generation (2.4 / 2.6 text).
        """
        raise NotImplementedError("Wire this to your LLM provider")
