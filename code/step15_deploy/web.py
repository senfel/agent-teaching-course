"""web.py — 第 15 期核心:把 Alex 跑成本地 Web 服务(FastAPI + SSE 流式响应)

累积式:step15 = step14 + 部署上线。前 14 期 Alex 只在命令行里交互,
这一期给它套一层 Web 外壳:
  - FastAPI 建服务,uvicorn 起进程,Docker 打包成一次性可跑镜像
  - 前端是一段内嵌 HTML(零依赖,打开 http://localhost:8000 即用)
  - 对话走 SSE(Server-Sent Events):调 stream_chat_with_tools(第 15 期新增),
    让 Alex 的每个字"边生成边到前端",工具调用也实时播报

为什么是 SSE 而不是 WebSocket?
  聊天是"单向流 + 每轮结束"的场景:客户端发一条消息,服务端持续吐多个
  事件直到 done。SSE 只需要 HTTP,浏览器原生 EventSource/fetch 流式读即可,
  不用像 WebSocket 那样维护双向长连接协议 —— 对"AI 打字机"这个需求,
  SSE 是最小够用的方案。

会话记录 + 会话列表:
  前端的左侧栏从 /sessions 拉历史会话表(读 raw_history.jsonl 按 session 聚合),
  点任意会话从 /history/{id} 回放该会话的 user/assistant 消息,并重建进程内
  上下文,做到"刷新/重启后还能接着聊"。

关键伏笔回收(第 14 期留的):
  GOAL_STATE 是模块级全局变量,天然跨 HTTP 请求存活。用户在 Web 里让
  Alex 订了目标契约后,刷网页、再开一个请求,/goal 端点读到的还是同一个
  目标 —— 这就是"无状态 HTTP + 有状态 Agent"的衔接点。
"""
import json

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

from .config import WEB_HOST, WEB_PORT, COMPACT_THRESHOLD_ROUNDS, RAW_HISTORY_PATH
from .prompt import build_system_prompt
from .memory import append_and_persist, trim_history, compact_history
from .stream import stream_chat_with_tools
from . import goal

app = FastAPI(title="Alex · 第 15 期 Web UI", docs_url=None, redoc_url=None)

# 进程内会话表:session_id → messages(每轮结束由 stream_chat_with_tools 落盘)
_SESSIONS: dict[str, list[dict]] = {}
_ROUNDS: dict[str, int] = {}


def _list_sessions() -> list[dict]:
    """从 raw_history.jsonl 聚合会话列表(事件溯源,天然可回放)。"""
    if not RAW_HISTORY_PATH.exists():
        return []
    sessions: dict[str, dict] = {}
    with RAW_HISTORY_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            sid = rec.get("session")
            if not sid:
                continue
            s = sessions.setdefault(sid, {
                "session_id": sid,
                "count": 0,
                "first_ts": rec.get("ts"),
                "last_ts": rec.get("ts"),
                "last_message": "",
            })
            s["count"] += 1
            s["last_ts"] = rec.get("ts") or s["last_ts"]
            if rec.get("role") == "user" and rec.get("content"):
                s["last_message"] = rec["content"]

    result = []
    for s in sessions.values():
        title = (s["last_message"] or "").replace("\n", " ").strip()
        if not title:
            title = "(空会话)"
        s["title"] = title[:30] + ("…" if len(title) > 30 else "")
        result.append(s)
    result.sort(key=lambda x: x["last_ts"] or "", reverse=True)
    return result


def _session_history(session_id: str) -> list[dict]:
    """读某个会话的 user/assistant 消息(用于前端回放历史)。"""
    if not RAW_HISTORY_PATH.exists():
        return []
    out: list[dict] = []
    with RAW_HISTORY_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("session") != session_id:
                continue
            if rec.get("role") in ("user", "assistant"):
                out.append({"role": rec["role"], "content": rec.get("content", "")})
    return out


def _ensure_session(session_id: str) -> list[dict]:
    """取(或重建)进程内上下文。首次/重启后从 raw_history 回放,做到可续聊。"""
    if session_id in _SESSIONS:
        return _SESSIONS[session_id]
    messages = [{"role": "system", "content": build_system_prompt()}]
    items = _session_history(session_id)
    for it in items:
        messages.append({"role": it["role"], "content": it["content"]})
    _SESSIONS[session_id] = messages
    _ROUNDS[session_id] = sum(1 for it in items if it["role"] == "user")
    return messages


def _get_messages(session_id: str) -> list[dict]:
    """按 session_id 取(或建)对话上下文。首次创建时组装 system prompt。"""
    return _ensure_session(session_id)


@app.get("/health")
def health():
    return {"status": "ok", "sessions": len(_SESSIONS)}


@app.get("/goal")
def get_goal():
    """读当前目标契约(跨请求存活)—— 第 14 期伏笔的 Web 版验证点。"""
    try:
        return json.loads(goal.goal_status())
    except Exception as e:
        return {"error": str(e)}


@app.get("/sessions")
def list_sessions():
    """会话列表(前端左侧栏)。"""
    return {"sessions": _list_sessions()}


@app.get("/history/{session_id}")
def session_history(session_id: str):
    """回放某个会话的 user/assistant 消息,并重建上下文以便续聊。"""
    _ensure_session(session_id)
    return {"session_id": session_id, "messages": _session_history(session_id)}


@app.get("/")
def index():
    return HTMLResponse(INDEX_HTML)


@app.post("/chat")
async def chat(payload: dict):
    message = (payload.get("message") or "").strip()
    session_id = payload.get("session_id") or "web-default"
    if not message:
        return {"error": "message 不能为空"}

    async def event_stream():
        messages = _get_messages(session_id)

        # 落盘用户消息(与 CLI 同款事件溯源)
        append_and_persist(messages, {"role": "user", "content": message}, session_id)
        _ROUNDS[session_id] = _ROUNDS.get(session_id, 0) + 1

        # 上下文管理:截断 + 压缩(复用记忆层,与 CLI 行为一致)
        messages[:] = trim_history(messages)
        if _ROUNDS[session_id] > COMPACT_THRESHOLD_ROUNDS:
            messages[:] = compact_history(messages)

        try:
            for ev in stream_chat_with_tools(messages, session_id):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'event': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 关掉反向代理缓冲,让 SSE 逐字到前端
        },
    )


def run_web():
    """启动 uvicorn,把 Alex 暴露到 http://localhost:8000(第 15 期默认入口)。"""
    import uvicorn
    print("=" * 60)
    print("第 15 期:部署上线 —— Alex 的 Web UI(流式响应)")
    print("=" * 60)
    print(f"浏览器打开: http://localhost:{WEB_PORT}")
    print("Ctrl-C 退出服务")
    print("=" * 60)
    try:
        uvicorn.run(app, host=WEB_HOST, port=WEB_PORT, log_level="info")
    except KeyboardInterrupt:
        # Ctrl-C 正常退出,别把 asyncio 的 KeyboardInterrupt 堆栈甩给用户
        print("\n[已停止] 服务已关闭,再见!")


# ============ 内嵌前端(单文件 HTML,零外部依赖)============
INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Alex · 第 15 期 Web UI</title>
<style>
  :root {
    --bg: #0f1629;
    --panel: #17203a;
    --border: #2a3557;
    --text: #e6ebff;
    --muted: #8b98c9;
    --accent: #5b8cff;
    --tool: #ffb454;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; height: 100vh; overflow: hidden; display: flex;
    background: var(--bg); color: var(--text);
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
  }
  .layout { display: flex; width: 100vw; height: 100vh; }

  /* 左侧会话列表 */
  #sidebar {
    width: 270px; flex-shrink: 0; background: var(--panel);
    border-right: 1px solid var(--border); display: flex; flex-direction: column;
  }
  .sidebar-head {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 16px; border-bottom: 1px solid var(--border);
    font-size: 14px; font-weight: 600;
  }
  .sidebar-head button {
    background: var(--accent); border: none; color: #fff; border-radius: 8px;
    width: 30px; height: 30px; cursor: pointer; font-size: 17px; line-height: 1;
  }
  #sessionList { flex: 1; overflow-y: auto; }
  .session-item {
    padding: 10px 14px; border-bottom: 1px solid rgba(42,53,87,0.6);
    cursor: pointer; transition: background 0.15s; border-left: 3px solid transparent;
  }
  .session-item:hover { background: rgba(91,140,255,0.08); }
  .session-item.active { background: rgba(91,140,255,0.16); border-left-color: var(--accent); }
  .session-item .t { font-size: 13px; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .session-item .m { font-size: 11px; color: var(--muted); margin-top: 4px; }
  .empty { padding: 20px 16px; font-size: 12px; color: var(--muted); text-align: center; }

  /* 右侧主区 */
  main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  header {
    padding: 14px 20px; background: linear-gradient(180deg, #16203a, #0f1629);
    border-bottom: 1px solid var(--border);
  }
  header h1 { margin: 0; font-size: 18px; }
  header p { margin: 4px 0 0; font-size: 12px; color: var(--muted); }
  #chat {
    flex: 1; overflow-y: auto; padding: 20px; display: flex;
    flex-direction: column; gap: 12px;
  }
  .row { display: flex; }
  .row.user { justify-content: flex-end; }
  .bubble {
    max-width: 76%; padding: 10px 14px; border-radius: 12px;
    line-height: 1.6; white-space: pre-wrap; word-break: break-word;
    font-size: 14px;
  }
  .row.user .bubble { background: var(--accent); color: #fff; border-bottom-right-radius: 4px; }
  .row.bot .bubble { background: var(--panel); border: 1px solid var(--border); border-bottom-left-radius: 4px; }
  .tool {
    align-self: flex-start; font-size: 12px; color: var(--tool);
    padding: 4px 10px; border: 1px dashed var(--tool); border-radius: 16px;
    background: rgba(255,180,84,0.08);
  }
  .cursor { display: inline-block; width: 7px; height: 15px; background: var(--muted); margin-left: 2px; animation: blink 1s infinite; vertical-align: -2px; }
  @keyframes blink { 0%,100% { opacity: 1; } 50% { opacity: 0; } }
  footer {
    display: flex; gap: 10px; padding: 14px; border-top: 1px solid var(--border);
    background: var(--panel);
  }
  textarea {
    flex: 1; resize: none; min-height: 44px; max-height: 160px;
    background: #0f1629; color: var(--text); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 12px; font-size: 14px;
    font-family: inherit; outline: none;
  }
  textarea:focus { border-color: var(--accent); }
  button {
    border: none; border-radius: 10px; padding: 0 20px; font-size: 14px;
    background: var(--accent); color: #fff; cursor: pointer; font-weight: 600;
  }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .actions { display: flex; gap: 8px; align-items: center; }
  .ghost { background: transparent; border: 1px solid var(--border); color: var(--muted); }
</style>
</head>
<body>
<div class="layout">
  <aside id="sidebar">
    <div class="sidebar-head">
      <span>🗂 会话</span>
      <button id="new" title="开一个新会话">＋</button>
    </div>
    <div id="sessionList"></div>
  </aside>

  <main>
    <header>
      <h1>Alex · 第 15 期 Web UI</h1>
      <p>流式响应(SSE)· FastAPI + uvicorn · 会话记录可回放 · 目标状态跨请求保存</p>
    </header>

    <div id="chat"></div>

    <footer>
      <textarea id="input" placeholder="和 Alex 说点什么…(Enter 发送,Shift+Enter 换行)" rows="1"></textarea>
      <div class="actions">
        <button id="goal" class="ghost" title="查看当前目标契约">🎯 目标</button>
        <button id="send">发送</button>
      </div>
    </footer>
  </main>
</div>

<script>
const chatEl = document.getElementById('chat');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send');
const listEl = document.getElementById('sessionList');
let sessionId = 'web-' + Date.now();
let streaming = false;

function addRow(role, text) {
  const row = document.createElement('div');
  row.className = 'row ' + (role === 'user' ? 'user' : 'bot');
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  row.appendChild(bubble);
  chatEl.appendChild(row);
  chatEl.scrollTop = chatEl.scrollHeight;
  return bubble;
}

function addTool(text) {
  const el = document.createElement('div');
  el.className = 'tool';
  el.textContent = text;
  chatEl.appendChild(el);
  chatEl.scrollTop = chatEl.scrollHeight;
}

function handleEvent(ev) {
  if (ev.event === 'delta') {
    if (!window._curBubble) window._curBubble = addRow('bot', '');
    window._curBubble.textContent += ev.text;
    chatEl.scrollTop = chatEl.scrollHeight;
  } else if (ev.event === 'tool_call') {
    let args = '';
    try { args = JSON.stringify(JSON.parse(ev.arguments || '{}')); } catch (e) { args = ev.arguments || ''; }
    if (args.length > 60) args = args.slice(0, 60) + '…';
    addTool('🔧 调用工具: ' + ev.name + (args ? ' · ' + args : ''));
  } else if (ev.event === 'tool_result') {
    let res = ev.result || '';
    if (res.length > 60) res = res.slice(0, 60) + '…';
    addTool('✅ ' + ev.name + ' 完成: ' + res);
  } else if (ev.event === 'done') {
    window._curBubble = null;
  } else if (ev.event === 'error') {
    addTool('⚠️ 出错: ' + ev.message);
    window._curBubble = null;
  }
}

async function loadSessions() {
  try {
    const res = await fetch('/sessions');
    const data = await res.json();
    renderSessionList(data.sessions || []);
  } catch (e) {}
}

function renderSessionList(sessions) {
  listEl.innerHTML = '';
  if (!sessions.length) {
    listEl.innerHTML = '<div class="empty">还没有会话记录<br>发一条消息就会出现在这里</div>';
    return;
  }
  for (const s of sessions) {
    const item = document.createElement('div');
    item.className = 'session-item' + (s.session_id === sessionId ? ' active' : '');
    item.onclick = () => openSession(s.session_id);
    const t = document.createElement('div');
    t.className = 't';
    t.textContent = s.title || '(无标题)';
    const m = document.createElement('div');
    m.className = 'm';
    const ts = (s.last_ts || '').replace('T', ' ').slice(0, 16);
    m.textContent = s.count + ' 条 · ' + ts;
    item.appendChild(t);
    item.appendChild(m);
    listEl.appendChild(item);
  }
}

async function openSession(id) {
  sessionId = id;
  chatEl.innerHTML = '';
  try {
    const res = await fetch('/history/' + encodeURIComponent(id));
    const data = await res.json();
    renderHistory(data.messages || []);
  } catch (e) {
    addTool('⚠️ 加载历史失败: ' + e.message);
  }
  loadSessions();  // 重绘左侧列表,高亮当前会话
}

function renderHistory(messages) {
  for (const m of messages) {
    const c = m.content || '';
    if (m.role === 'user') {
      addRow('user', c);
    } else if (c.startsWith('[tool_calls:')) {
      addTool('🔧 ' + c);
    } else if (c) {
      addRow('bot', c);
    }
  }
  chatEl.scrollTop = chatEl.scrollHeight;
}

async function send() {
  const message = inputEl.value.trim();
  if (!message || streaming) return;
  addRow('user', message);
  inputEl.value = '';
  inputEl.style.height = 'auto';
  window._curBubble = null;
  streaming = true;
  sendBtn.disabled = true;
  try {
    const res = await fetch('/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: message, session_id: sessionId }),
    });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf('\\n\\n')) !== -1) {
        const chunk = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        for (const line of chunk.split('\\n')) {
          if (!line.startsWith('data: ')) continue;
          const payload = line.slice(6);
          if (payload === '[DONE]') continue;
          try { handleEvent(JSON.parse(payload)); } catch (e) {}
        }
      }
    }
  } catch (e) {
    addTool('⚠️ 请求失败: ' + e.message);
  } finally {
    streaming = false;
    sendBtn.disabled = false;
    window._curBubble = null;
    inputEl.focus();
    loadSessions();  // 一轮对话结束,刷新左侧会话列表
  }
}

async function showGoal() {
  const res = await fetch('/goal');
  const data = await res.json();
  addTool('🎯 ' + JSON.stringify(data));
}

function newSession() {
  sessionId = 'web-' + Date.now();
  chatEl.innerHTML = '';
  addTool('🆕 已开新会话');
  loadSessions();
}

sendBtn.addEventListener('click', send);
inputEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
document.getElementById('goal').addEventListener('click', showGoal);
document.getElementById('new').addEventListener('click', newSession);

loadSessions();
</script>
</body>
</html>
"""