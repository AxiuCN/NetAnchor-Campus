"""验证码识别：默认进程内懒加载（模型只在首次识别时载入）。

识别结果只保留需要的字符；位数不对由上层决定是否重取验证码。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def normalize(text: str, digits_only: bool = True) -> str:
    if not text:
        return ""
    cleaned = text.strip()
    if digits_only:
        cleaned = "".join(ch for ch in cleaned if ch.isdigit())
    return cleaned


class OcrEngine:
    """进程内识别器。构造不加载模型，首次 `recognize` 才导入 ddddocr 并初始化。"""

    def __init__(self, digits_only: bool = True) -> None:
        self._digits_only = digits_only
        self._engine = None
        self._initialized = False

    @property
    def initialized(self) -> bool:
        return self._initialized

    def _ensure_engine(self):
        if self._engine is None:
            import ddddocr  # 重依赖：仅在真正需要识别时导入

            engine = ddddocr.DdddOcr(show_ad=False)
            if self._digits_only:
                # 限定字符集后单张准确率明显提升（spike：82.5% → 92.5%）
                engine.set_ranges("0123456789")
            self._engine = engine
            self._initialized = True
            logger.info("OCR 模型已加载（进程内）")
        return self._engine

    def recognize(self, image: bytes) -> str:
        engine = self._ensure_engine()
        return normalize(engine.classification(image), digits_only=self._digits_only)

    def __call__(self, image: bytes) -> str:
        return self.recognize(image)
