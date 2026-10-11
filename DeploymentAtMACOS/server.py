"""PI Agent 网页后端 —— 只用 Python 标准库，零依赖。

运行：
    python3 server.py              # 默认 http://localhost:8000
    python3 server.py --port 7100  # 指定端口
    PORT=7100 python3 server.py    # 或用环境变量
"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pi_agent import PiAgent

BASE_DIR = Path(__file__).resolve().parent
agent = PiAgent(BASE_DIR / "notes")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send("text/html; charset=utf-8",
                       (BASE_DIR / "index.html").read_bytes())
        elif self.path == "/api/profile":
            payload = json.dumps({"profile": agent.profile},
                                 ensure_ascii=False).encode()
            self._send("application/json; charset=utf-8", payload)
        elif self.path == "/api/scene":
            payload = json.dumps({"scene": agent.scene_display(),
                                  "bg": agent.current_bg,
                                  "story": agent.story_state()},
                                 ensure_ascii=False).encode()
            self._send("application/json; charset=utf-8", payload)
        elif self.path.startswith("/api/tts"):
            from urllib.parse import parse_qs, urlparse
            text = parse_qs(urlparse(self.path).query).get("text", [""])[0]
            try:
                name = agent.speak(text)
                payload = json.dumps({"audio": name},
                                     ensure_ascii=False).encode()
                self._send("application/json; charset=utf-8", payload)
            except Exception as e:
                payload = json.dumps({"audio": None, "error": str(e)},
                                     ensure_ascii=False).encode()
                self._send("application/json; charset=utf-8", payload, 500)
        elif self.path.startswith("/api/assets/"):
            name = Path(self.path[len("/api/assets/"):]).name  # 防路径穿越
            f = agent.assets_dir / name
            mime = {".svg": "image/svg+xml", ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg", ".png": "image/png",
                    ".mp3": "audio/mpeg"}.get(f.suffix)
            if mime and f.exists():
                self._send(mime, f.read_bytes())
            else:
                self._send("text/plain; charset=utf-8", b"404 Not Found", 404)
        else:
            self._send("text/plain; charset=utf-8", b"404 Not Found", 404)

    def do_POST(self):
        # ---- 画像采集阶段 ----
        if self.path == "/api/profile/reset":
            agent.reset_session()
            self._send("application/json; charset=utf-8",
                       json.dumps({"ok": True}).encode())
            return
        if self.path == "/api/profile_chat":
            try:
                body = json.loads(
                    self.rfile.read(int(self.headers.get("Content-Length", 0)))
                    or b"{}")
                reply, count = agent.profile_chat(str(body.get("message", "")))
                payload = json.dumps({"reply": reply, "count": count},
                                     ensure_ascii=False).encode()
                self._send("application/json; charset=utf-8", payload)
            except Exception as e:
                payload = json.dumps({"reply": f"❌ 出错：{e}", "count": 0},
                                     ensure_ascii=False).encode()
                self._send("application/json; charset=utf-8", payload, 500)
            return
        if self.path == "/api/profile_done":
            try:
                profile = agent.finalize_profile()
                payload = json.dumps(
                    {"ok": True, "profile": profile,
                     "story": agent.story_state()},
                    ensure_ascii=False).encode()
                self._send("application/json; charset=utf-8", payload)
            except Exception as e:
                payload = json.dumps({"ok": False, "error": str(e)},
                                     ensure_ascii=False).encode()
                self._send("application/json; charset=utf-8", payload, 500)
            return
        if self.path == "/api/story/choice":
            try:
                body = json.loads(
                    self.rfile.read(int(self.headers.get("Content-Length", 0)))
                    or b"{}")
                state = agent.story_choose(int(body.get("index", -1)))
                payload = json.dumps({"ok": True, "story": state},
                                     ensure_ascii=False).encode()
                self._send("application/json; charset=utf-8", payload)
            except Exception as e:
                payload = json.dumps({"ok": False, "error": str(e)},
                                     ensure_ascii=False).encode()
                self._send("application/json; charset=utf-8", payload, 500)
            return
        # ---- 交互阶段 ----
        if self.path != "/api/chat":
            self._send("text/plain; charset=utf-8", b"404 Not Found", 404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            reply = agent.reply(str(body.get("message", "")))
            payload = json.dumps({"reply": reply,
                                  "scene": agent.scene_display(),
                                  "bg": agent.current_bg,
                                  "story": agent.story_state()},
                                 ensure_ascii=False).encode()
            self._send("application/json; charset=utf-8", payload)
        except Exception as e:  # 出错也返回可读信息，网页不会卡死
            payload = json.dumps({"reply": f"❌ 出错：{e}"},
                                 ensure_ascii=False).encode()
            self._send("application/json; charset=utf-8", payload, 500)

    def _send(self, content_type, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # 静默访问日志
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", 8000)))
    args, _ = parser.parse_known_args()  # 忽略 npm 透传的其他参数

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"PI Agent 已启动 → http://localhost:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
