"""Codex Responses → Chat/Completions 适配代理

Codex CLI 只支持 /v1/responses 格式。
Kimi/GLM 只支持 /v1/chat/completions 格式。
这个代理坐中间翻译。

用法: python codex_proxy.py --port 5678 --target https://api.moonshot.cn/v1
启动后 Codex 连 http://127.0.0.1:5678/v1 即可用 Kimi。
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
import os


class ProxyHandler(BaseHTTPRequestHandler):
    target_base: str = ""
    target_key: str = ""

    def do_POST(self):
        if self.path not in ("/v1/responses", "/responses"):
            self.send_error(404)
            return

        # 读 Codex 发来的 responses 格式请求
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length > 0 else {}

        # 翻译 responses → chat/completions
        chat_body = self._translate_request(body)

        # 转发到目标 API
        try:
            data = json.dumps(chat_body).encode()
            req = urllib.request.Request(
                f"{self.target_base}/chat/completions",
                data=data, method="POST",
                headers={
                    "Authorization": f"Bearer {self.target_key}",
                    "Content-Type": "application/json",
                },
            )
            resp = urllib.request.urlopen(req, timeout=120)
            chat_resp = json.loads(resp.read())
            # 翻译 chat/completions → responses
            out = self._translate_response(chat_resp, body.get("model", ""))
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(e.read())
            return
        except Exception as e:
            self.send_error(502, str(e))
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(out).encode())

    def _translate_request(self, body: dict) -> dict:
        """Codex responses → chat/completions"""
        chat = {"model": body.get("model", ""), "messages": [], "max_tokens": body.get("max_output_tokens", 4096)}
        if "temperature" in body:
            chat["temperature"] = body["temperature"]

        # input → messages
        inp = body.get("input", [])
        if isinstance(inp, str):
            chat["messages"] = [{"role": "user", "content": inp}]
        elif isinstance(inp, list):
            for item in inp:
                role = item.get("role", "user")
                content = item.get("content", "")
                if isinstance(content, list):
                    # 取第一个 text
                    content = " ".join(c.get("text", "") for c in content if c.get("type") == "input_text")
                chat["messages"].append({"role": role, "content": content})

        # tools → tools (格式相同)
        if "tools" in body:
            chat["tools"] = body["tools"]
            chat["tool_choice"] = body.get("tool_choice", "auto")

        return chat

    def _translate_response(self, chat_resp: dict, model: str) -> dict:
        """chat/completions → Codex responses"""
        choice = chat_resp.get("choices", [{}])[0]
        msg = choice.get("message", {})
        finish = choice.get("finish_reason", "stop")

        output = []
        # text content
        if msg.get("content"):
            output.append({"type": "message", "role": "assistant",
                          "content": [{"type": "output_text", "text": msg["content"]}]})

        # tool calls
        for tc in msg.get("tool_calls", []):
            func = tc.get("function", {})
            output.append({
                "type": "function_call",
                "id": tc.get("id", ""),
                "call_id": tc.get("id", ""),
                "name": func.get("name", ""),
                "arguments": func.get("arguments", ""),
            })

        return {
            "id": chat_resp.get("id", ""),
            "model": model,
            "output": output,
            "usage": chat_resp.get("usage", {}),
            "status": "completed" if finish == "stop" else "incomplete",
        }

    def log_message(self, format, *args):
        print(f"[proxy] {args[0]}" if args else "", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Codex Responses → Chat/Completions 适配代理")
    parser.add_argument("--port", type=int, default=5678)
    parser.add_argument("--target", default="https://api.moonshot.cn/v1")
    parser.add_argument("--key", default="")
    args = parser.parse_args()

    key = args.key or os.environ.get("KIMI_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("ZHIPU_API_KEY") or ""
    if not key:
        print("需要 --key 或设置 KIMI_API_KEY/ZHIPU_API_KEY 环境变量", file=sys.stderr)
        sys.exit(1)

    ProxyHandler.target_base = args.target.rstrip("/")
    ProxyHandler.target_key = key

    server = HTTPServer(("127.0.0.1", args.port), ProxyHandler)
    print(f"代理启动: 127.0.0.1:{args.port} → {args.target}", file=sys.stderr)
    print(f"模型: 由 Codex 传入", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
