"""DeepSeek API 独立测试程序（阶段四）。

在把它接进主流程之前，单独验证三件事：

1. 密钥与配置是否正确（只打印掩码，绝不打印明文）；
2. 能否正常调用对话接口（网络是否通、额度是否够）；
3. 能否稳定拿到**结构化 JSON** 输出 —— 后面"讲稿拆段 + 生成提示词"全靠这一点。

用法（在项目根目录执行）::

    .venv\\Scripts\\python.exe scripts\\test_deepseek.py                # 跑完整测试
    .venv\\Scripts\\python.exe scripts\\test_deepseek.py --list-models  # 只看账号可用模型
    .venv\\Scripts\\python.exe scripts\\test_deepseek.py --model deepseek-chat
    .venv\\Scripts\\python.exe scripts\\test_deepseek.py --offline      # 不联网，只做本地检查

说明：本程序只读 .env 中的密钥，不会写入或打印明文。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# 统一按 UTF-8 输出，避免中文在重定向时乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import requests  # noqa: E402

from video_auto import config as cfg  # noqa: E402

OK, WARN, ERR = "OK", "警告", "错误"


def say(level: str, title: str, detail: str = "") -> None:
    print(f"[{level}] {title}")
    if detail:
        for line in str(detail).splitlines():
            print(f"        {line}")


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _explain_http_error(status: int, body: str) -> str:
    """把 API 的 HTTP 错误翻译成"你该做什么"。"""
    table = {
        400: "请求格式有问题。检查 model 名称与 messages 结构。",
        401: "密钥无效或已过期。去 DeepSeek 后台重新生成密钥，更新 .env 里的 DEEPSEEK_API_KEY。",
        402: "账户余额不足。请到 DeepSeek 后台充值后再试。",
        403: "该密钥无权访问此接口或模型。确认密钥权限与模型名。",
        404: "接口地址或模型名不存在。检查 config/settings.yaml 里的 deepseek.base_url 与 model。",
        429: "请求过于频繁或已超限。降低频率、稍后重试，或检查账户限流设置。",
        500: "DeepSeek 服务端错误。稍后重试。",
        503: "DeepSeek 服务暂时不可用。稍后重试。",
    }
    hint = table.get(status, "未知状态码，请把下面的返回内容发给开发者。")
    return f"HTTP {status}：{hint}\n服务端返回：{body[:400]}"


def list_models(base_url: str, key: str, timeout: int) -> list[str] | None:
    """列出账号可用的模型名。"""
    url = base_url.rstrip("/") + "/models"
    try:
        resp = requests.get(url, headers=_headers(key), timeout=timeout)
    except requests.RequestException as exc:
        say(ERR, "无法连接 DeepSeek 接口", f"地址：{url}\n原因：{exc}\n"
            "解决办法：确认能上网（浏览器打开 platform.deepseek.com 试试），或公司网络/代理是否拦截。")
        return None
    if resp.status_code != 200:
        say(ERR, "列出模型失败", _explain_http_error(resp.status_code, resp.text))
        return None
    data = resp.json().get("data", [])
    ids = [item.get("id", "?") for item in data]
    if not ids:
        say(WARN, "接口返回的模型列表为空", resp.text[:300])
        return []
    say(OK, f"账号可用模型共 {len(ids)} 个", "、".join(ids))
    return ids


def chat(
    base_url: str,
    key: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    timeout: int,
    retries: int,
    json_mode: bool = False,
) -> dict | None:
    """调用一次对话接口。失败返回 None，并打印可执行的解决建议。"""
    url = base_url.rstrip("/") + "/chat/completions"
    payload: dict = {"model": model, "messages": messages, "stream": False}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    last_error = ""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, headers=_headers(key), json=payload, timeout=timeout)
        except requests.Timeout:
            last_error = f"请求超时（{timeout} 秒）"
        except requests.RequestException as exc:
            last_error = f"网络错误：{exc}"
        else:
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 500, 503) and attempt < retries:
                wait = 2 ** attempt
                say(WARN, f"第 {attempt} 次调用失败，{wait} 秒后重试",
                    _explain_http_error(resp.status_code, resp.text))
                time.sleep(wait)
                continue
            say(ERR, f"调用失败（尝试 {attempt}/{retries} 次）",
                _explain_http_error(resp.status_code, resp.text))
            return None
        if attempt < retries:
            wait = 2 ** attempt
            say(WARN, f"第 {attempt} 次调用失败：{last_error}，{wait} 秒后重试")
            time.sleep(wait)
    say(ERR, "多次重试后仍然失败", f"最后一次错误：{last_error}")
    return None


SAMPLE_SCRIPT = (
    "很多人以为学编程要先学一堆数学。其实第一步是让电脑替你把重复劳动干掉。"
    "今天就从一个能自动整理下载文件夹的小脚本开始。"
)

SPLIT_SYSTEM_PROMPT = (
    "你是短视频分镜编剧。用户会给你一段中文讲稿，你要把它拆成适合 AI 视频生成的若干片段。"
    "严格遵守：只输出 JSON，不要输出任何解释文字、不要用 Markdown 代码块包裹。"
    'JSON 结构：{"segments": [{"index": 1, "summary": "中文一句话概括", '
    '"prompt": "English prompt for the video model", "seconds": 5}]}'
)


def test_json_output(
    base_url: str, key: str, model: str, *, timeout: int, retries: int
) -> bool:
    """核心测试：把讲稿拆段，要求返回可解析的结构化 JSON。"""
    messages = [
        {"role": "system", "content": SPLIT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"请把下面的讲稿拆成 2 到 3 个片段，每个片段 5 秒左右：\n\n{SAMPLE_SCRIPT}",
        },
    ]
    data = chat(base_url, key, model, messages, timeout=timeout, retries=retries, json_mode=True)
    if data is None:
        say(ERR, "结构化 JSON 测试：调用失败")
        return False

    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    usage = data.get("usage", {})
    say(OK, "已收到模型返回",
        f"token 用量：输入 {usage.get('prompt_tokens', '?')} / 输出 {usage.get('completion_tokens', '?')}")

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        say(ERR, "返回内容不是合法 JSON", f"解析器报错：{exc}\n原始内容片段：\n{content[:600]}")
        return False

    segments = parsed.get("segments")
    if not isinstance(segments, list) or not segments:
        say(ERR, "JSON 结构不符合预期：缺少非空的 segments 数组", json.dumps(parsed, ensure_ascii=False)[:400])
        return False

    problems: list[str] = []
    for i, item in enumerate(segments, start=1):
        if not isinstance(item, dict):
            problems.append(f"第 {i} 项不是对象")
            continue
        if not isinstance(item.get("index"), int):
            problems.append(f"第 {i} 项 index 不是整数")
        for field in ("summary", "prompt"):
            if not isinstance(item.get(field), str) or not item.get(field, "").strip():
                problems.append(f"第 {i} 项 {field} 缺失或为空")

    if problems:
        say(ERR, "JSON 字段校验未通过", "\n".join(problems))
        return False

    lines = [f"共 {len(segments)} 段："]
    for item in segments:
        lines.append(f"  [{item['index']}] {item['summary']}")
        lines.append(f"      prompt: {item['prompt']}")
    say(OK, "结构化 JSON 测试通过（字段齐全、可解析）", "\n".join(lines))
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepSeek API 独立测试（阶段四）")
    parser.add_argument("--model", help="覆盖 config/settings.yaml 里的模型名")
    parser.add_argument("--list-models", action="store_true", help="只列出账号可用模型")
    parser.add_argument("--offline", action="store_true", help="不联网，只做本地配置检查")
    args = parser.parse_args()

    print("=" * 68)
    print("DeepSeek API 独立测试（阶段四）")
    print("=" * 68)

    try:
        settings = cfg.load_settings()
        base_url = str(settings.get("deepseek.base_url"))
        model = args.model or str(settings.get("deepseek.model"))
        timeout = int(settings.get("deepseek.timeout_seconds", 120))
        retries = int(settings.get("deepseek.max_retries", 3))
    except cfg.ConfigError as exc:
        print(f"[错误] {exc}")
        return 2

    print(f"接口地址：{base_url}")
    print(f"使用模型：{model}")

    try:
        key = cfg.get_secret("DEEPSEEK_API_KEY")
    except cfg.ConfigError as exc:
        print(f"[错误] {exc}")
        return 2
    say(OK, "密钥已从 .env 读取", f"DEEPSEEK_API_KEY = {cfg.mask_secret(key)}（掩码显示）")

    if args.offline:
        say(WARN, "已跳过联网测试", "去掉 --offline 即可真正调用接口。")
        return 0

    print("-" * 68)
    ids = list_models(base_url, key, timeout)
    if args.list_models:
        return 0 if ids is not None else 1
    if ids is not None and model not in ids:
        say(WARN, f"配置的模型 {model} 不在账号可用模型列表中",
            "若下一步调用失败，请用 --model 指定上面列出的某个模型名。")

    print("-" * 68)
    say(WARN, "开始最小对话测试（会产生少量 token 消耗）")
    data = chat(base_url, key, model, [{"role": "user", "content": "请只回复两个字：正常"}],
                timeout=timeout, retries=retries)
    if data is None:
        say(ERR, "最小对话测试失败")
        return 1
    reply = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    say(OK, "最小对话测试通过", f"模型回复：{reply.strip()[:80]}")

    print("-" * 68)
    say(WARN, "开始结构化 JSON 测试（讲稿拆段）")
    if not test_json_output(base_url, key, model, timeout=timeout, retries=retries):
        say(ERR, "阶段四未通过", "请把上面的错误信息发给开发者。")
        return 1

    print("-" * 68)
    say(OK, "阶段四全部通过", "DeepSeek 接口可用，且能稳定返回结构化 JSON。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
