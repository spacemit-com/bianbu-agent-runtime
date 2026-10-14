"""Provider environment names scrubbed from Tool subprocesses."""

from __future__ import annotations


SUPPORTED_PROVIDER_ENV_NAMES: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "anthropic": (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
    ),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GEMINI_BASE_URL"),
    "alibaba": ("DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL"),
    "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"),
    "minimax": ("MINIMAX_API_KEY", "MINIMAX_BASE_URL"),
    "zhipu": ("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY", "GLM_BASE_URL"),
    "kimi": ("KIMI_API_KEY", "KIMI_CODING_API_KEY", "KIMI_BASE_URL"),
    "xai": ("XAI_API_KEY", "XAI_BASE_URL"),
    "huggingface": ("HF_TOKEN", "HF_BASE_URL"),
    "aws": ("AWS_BEARER_TOKEN_BEDROCK", "BEDROCK_BASE_URL"),
    "lmstudio": ("LM_API_KEY", "LM_BASE_URL"),
    "copilot": (
        "COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN",
        "COPILOT_API_BASE_URL", "COPILOT_ACP_BASE_URL",
    ),
    "stepfun": ("STEPFUN_API_KEY", "STEPFUN_BASE_URL"),
    "arcee": ("ARCEEAI_API_KEY", "ARCEE_BASE_URL"),
    "gmi": ("GMI_API_KEY", "GMI_BASE_URL"),
    "nvidia": ("NVIDIA_API_KEY", "NVIDIA_BASE_URL"),
    "opencode": (
        "OPENCODE_ZEN_API_KEY", "OPENCODE_ZEN_BASE_URL",
        "OPENCODE_GO_API_KEY", "OPENCODE_GO_BASE_URL",
    ),
    "kilocode": ("KILOCODE_API_KEY", "KILOCODE_BASE_URL"),
    "xiaomi": ("XIAOMI_API_KEY", "XIAOMI_BASE_URL"),
    "tencent": ("TOKENHUB_API_KEY", "TOKENHUB_BASE_URL"),
    "ollama": ("OLLAMA_API_KEY", "OLLAMA_BASE_URL"),
    "azure": ("AZURE_FOUNDRY_API_KEY", "AZURE_FOUNDRY_BASE_URL"),
    "minimax-cn": ("MINIMAX_CN_API_KEY", "MINIMAX_CN_BASE_URL"),
    "kimi-cn": ("KIMI_CN_API_KEY",),
    "alibaba-coding": (
        "ALIBABA_CODING_PLAN_API_KEY", "ALIBABA_CODING_PLAN_BASE_URL",
    ),
}
PROVIDER_SUBPROCESS_PASSTHROUGH_ENV_NAMES = frozenset(
    {"CLAUDE_CODE_OAUTH_TOKEN"}
)


__all__ = [
    "PROVIDER_SUBPROCESS_PASSTHROUGH_ENV_NAMES",
    "SUPPORTED_PROVIDER_ENV_NAMES",
]
