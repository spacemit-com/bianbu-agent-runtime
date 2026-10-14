"""Typed model recovery failures, independent of the engine and provider."""


class ModelRecoveryExhausted(RuntimeError):
    def __init__(self, reason: str, attempts: int, diagnostic: str) -> None:
        self.reason = reason
        self.attempts = attempts
        self.diagnostic = diagnostic
        super().__init__(f"自动恢复未能完成当前任务（{diagnostic}）。")

    def error_payload(self, *, resumable: bool) -> dict:
        message = str(self)
        if resumable:
            message += "已完成的步骤和文件仍保留，可在当前会话继续处理。"
        return {
            "code": "model_recovery_exhausted", "message": message,
            "details": {"reason": self.reason, "attempts": self.attempts,
                        "resumable": resumable},
        }
