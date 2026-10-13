"""DeepSeek API 客户端（阶段八起复用）。

与 ``scripts/test_deepseek.py`` 的探测逻辑同源，但这里封装成可复用类：
密钥只从 ``.env``/环境变量读取，任何日志与异常里都只出现掩码。
"""

from __future__ import annotations

import time
from typing import Any

import requests

from . import config as cfg


class DeepSeekError(RuntimeError):
    """调用 DeepSeek 失败，错误信息需包含可执行的解决建议。"""


_HTTP_HINTS = {
    400: "请求格式有问题。检查 model 名称与 messages 结构。",
    401: "密钥无效或已过期。去 DeepSeek 后台重新生成密钥，更新 .env 里的 DEEPSEEK_API_KEY。",
    402: "账户余额不足。请到 DeepSeek 后台充值后再试。",
    403: "该密钥无权访问此接口或模型。确认密钥权限与模型名。",
    404: "接口地址或模型名不存在。检查 config/settings.yaml 里的 deepseek.base_url 与 model。",
    429: "请求过于频繁或已超限。稍后重试。",
    500: "DeepSeek 服务端错误，稍后重试。",
    503: "DeepSeek 服务暂时不可用，稍后重试。",
}


class DeepSeekClient:
    def __init__(self, settings: cfg.Settings | None = None, *, api_key: str | None = None) -> None:
        self.settings = settings or cfg.load_settings()
        self.base_url = str(self.settings.get("deepseek.base_url")).rstrip("/")
        self.model = str(self.settings.get("deepseek.model"))
        self.timeout = int(self.settings.get("deepseek.timeout_seconds", 120))
        self.retries = max(1, int(self.settings.get("deepseek.max_retries", 3)))
        self.api_key = api_key or cfg.get_secret("DEEPSEEK_API_KEY")

    # ---------------- 基础 ----------------

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def list_models(self) -> list[str]:
        try:
            resp = requests.get(f"{self.base_url}/models", headers=self._headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise DeepSeekError(f"无法连接 DeepSeek：{exc}\n解决办法：检查网络与代理设置。") from exc
        if resp.status_code != 200:
            raise DeepSeekError(
                f"列出模型失败（HTTP {resp.status_code}）：{_HTTP_HINTS.get(resp.status_code, '')}\n"
                f"{resp.text[:300]}"
            )
        return [str(item.get("id")) for item in resp.json().get("data", [])]

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """调用对话接口，返回正文文本。失败时抛 :class:`DeepSeekError`。"""
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        last_error = ""
        for attempt in range(1, self.retries + 1):
            try:
                resp = requests.post(f"{self.base_url}/chat/completions",
                                     headers=self._headers, json=payload, timeout=self.timeout)
            except requests.Timeout:
                last_error = f"请求超时（{self.timeout} 秒）"
            except requests.RequestException as exc:
                last_error = f"网络错误：{exc}"
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices") or []
                    if not choices:
                        raise DeepSeekError(f"接口没有返回内容：{str(data)[:300]}")
                    return str(choices[0].get("message", {}).get("content", ""))
                if resp.status_code in (429, 500, 503) and attempt < self.retries:
                    time.sleep(2 ** attempt)
                    continue
                raise DeepSeekError(
                    f"调用失败（HTTP {resp.status_code}）：{_HTTP_HINTS.get(resp.status_code, '')}\n"
                    f"{resp.text[:300]}"
                )
            if attempt < self.retries:
                time.sleep(2 ** attempt)
        raise DeepSeekError(f"重试 {self.retries} 次后仍失败：{last_error}")
