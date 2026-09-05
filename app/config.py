"""Runtime configuration. All values have safe defaults so the app runs with zero setup."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    # Simulation
    tick_seconds: float = float(_env("TICK_SECONDS", "0.6"))
    txns_per_tick: int = int(_env("TXNS_PER_TICK", "45"))
    window_size: int = int(_env("WINDOW_SIZE", "2500"))   # rolling txns kept in memory
    recent_window: int = int(_env("RECENT_WINDOW", "900"))  # slice used for current-state stats

    # Detection thresholds
    min_segment_samples: int = int(_env("MIN_SEGMENT_SAMPLES", "40"))
    z_threshold: float = float(_env("Z_THRESHOLD", "3.0"))  # 2-proportion z-test
    min_absolute_drop: float = float(_env("MIN_ABS_DROP", "0.12"))  # 12 pts

    # LLM (optional). provider in {none, anthropic, openai, gemini}
    llm_provider: str = _env("LLM_PROVIDER", "none").lower()
    llm_model: str = _env("LLM_MODEL", "")
    llm_timeout: float = float(_env("LLM_TIMEOUT", "20"))

    # Money
    currency: str = _env("CURRENCY", "INR")
    currency_symbol: str = _env("CURRENCY_SYMBOL", "\u20b9")  # ₹

    def api_key(self) -> str:
        return {
            "anthropic": os.environ.get("ANTHROPIC_API_KEY", ""),
            "openai": os.environ.get("OPENAI_API_KEY", ""),
            "gemini": os.environ.get("GEMINI_API_KEY", ""),
        }.get(self.llm_provider, "")

    def default_model(self) -> str:
        if self.llm_model:
            return self.llm_model
        return {
            "anthropic": "claude-3-5-haiku-latest",
            "openai": "gpt-4o-mini",
            "gemini": "gemini-1.5-flash",
        }.get(self.llm_provider, "")

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider != "none" and bool(self.api_key())


settings = Settings()
