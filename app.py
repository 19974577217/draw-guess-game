# app.py - 你画我猜游戏后端（完整最终版）
# 运行方式: pip install fastapi uvicorn websockets
# uvicorn app:app --host 0.0.0.0 --port 5000 --reload

import json
import os
import time
import uuid
import asyncio
import random
import hashlib
from typing import Dict, List, Any, Optional, Set
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pydantic import BaseModel

# ==================== 配置 ====================
DATA_FILE = "whiteboard_data.json"  # 保存所有路径的JSON文件
WORDS_FILE = "words.json"  # 词库文件
BAN_FILE = "banned_devices.json"  # 封禁设备记录文件
CLEANUP_INTERVAL = 10  # 清理任务间隔（秒）
EXPIRY_SECONDS = 180  # 3分钟无活动自动清除笔迹
ADMIN_PASSWORD = "Mm881717"  # 管理员密码
MAX_LOGIN_ATTEMPTS = 3  # 最大登录尝试次数
LOGIN_BLOCK_SECONDS = 60  # 登录失败封禁秒数


# ==================== 全局数据结构 ====================
class AppState:
    def __init__(self):
        self.all_paths: List[Dict[str, Any]] = []  # 所有有效路径
        self.active_connections: Dict[str, WebSocket] = {}  # user_id -> websocket
        self.user_last_active: Dict[str, float] = {}  # 最后活跃时间戳
        self.user_colors: Dict[str, str] = {}  # 用户ID -> 颜色
        self.user_names: Dict[str, str] = {}  # 用户ID -> 昵称
        self.user_last_chat: Dict[str, float] = {}  # 用户最后聊天时间
        self.user_device_map: Dict[str, str] = {}  # user_id -> device_id
        self.user_paths: Dict[str, List[int]] = {}  # 用户ID -> 路径索引列表（用于撤销）
        self.rooms: Dict[str, Any] = {}  # 房间ID -> 房间信息
        self.user_rooms: Dict[str, str] = {}  # 用户ID -> 房间ID
        self.room_messages: Dict[str, List[Dict]] = {}  # 房间ID -> 消息列表
        self.banned_devices: Dict[str, Dict] = {}  # 封禁设备记录
        self.admin_attempts: Dict[str, Dict] = {}  # 管理员登录尝试记录


# 创建全局状态实例
state = AppState()

# 预定义颜色列表
COLORS = [
    "#FF6B6B", "#4ECDC4", "#45B7D1", "#96CEB4", "#FFEEAD",
    "#D4A5A5", "#9B59B6", "#3498DB", "#E67E22", "#2ECC71",
    "#E74C3C", "#F1C40F", "#1ABC9C", "#E67E22", "#95A5A6",
    "#34495E", "#27AE60", "#8E44AD", "#F39C12", "#16A085"
]


def get_random_color():
    """返回随机颜色"""
    return random.choice(COLORS)


def get_random_name():
    """生成随机昵称"""
    adjectives = ["快乐的", "聪明的", "可爱的", "勇敢的", "机灵的", "活泼的", "温柔的", "酷酷的", "神秘的", "热情的"]
    nouns = ["画家", "玩家", "画手", "猜手", "大师", "新手", "高手", "路人", "观众", "选手"]
    return f"{random.choice(adjectives)}{random.choice(nouns)}{random.randint(1, 999)}"


def generate_room_code():
    """生成三位数房间号（000-999）"""
    while True:
        code = f"{random.randint(0, 999):03d}"
        if code not in state.rooms:
            return code


def load_words():
    """加载词库"""
    default_words = ["苹果", "香蕉", "梨子", "电视机", "电脑", "手机", "汽车", "飞机", "房子", "树木", "花朵", "太阳",
                     "月亮", "星星", "云朵", "雨水", "河流", "海洋", "山脉", "城市"]

    if not os.path.exists(WORDS_FILE):
        with open(WORDS_FILE, 'w', encoding='utf-8') as f:
            json.dump(default_words, f, ensure_ascii=False, indent=2)
        return default_words

    try:
        with open(WORDS_FILE, 'r', encoding='utf-8') as f:
            words = json.load(f)
            if not words:
                return default_words
            return words
    except:
        return default_words


def load_banned_devices():
    """加载封禁设备列表"""
    if not os.path.exists(BAN_FILE):
        return

    try:
        with open(BAN_FILE, 'r', encoding='utf-8') as f:
            bans = json.load(f)
            now = time.time()
            for device_id, ban_info in bans.items():
                if ban_info.get("duration") == 0 or now < ban_info["expires_at"]:
                    state.banned_devices[device_id] = ban_info
    except Exception as e:
        print(f"加载封禁列表失败: {e}")


def save_banned_devices():
    """保存封禁设备列表"""
    try:
        with open(BAN_FILE, 'w', encoding='utf-8') as f:
            json.dump(state.banned_devices, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存封禁列表失败: {e}")


# 词库
WORD_LIST = load_words()


def get_random_word():
    """随机获取一个词"""
    return random.choice(WORD_LIST)


def calculate_word_match(guess: str, answer: str) -> int:
    """计算猜词匹配度（0-100）"""
    if not guess or not answer:
        return 0

    guess = guess.lower()
    answer = answer.lower()

    # 完全匹配
    if guess == answer:
        return 100

    # 计算公共子序列长度
    m, n = len(guess), len(answer)
    dp = [[0] * (n + 1) for _ in range(m + 1)]

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if guess[i - 1] == answer[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])

    lcs = dp[m][n]
    return int((lcs / max(len(answer), 1)) * 100)


# ==================== JSON文件存储 ====================
def load_paths_from_file():
    """从JSON文件加载所有路径"""
    if not os.path.exists(DATA_FILE):
        state.all_paths = []
        return

    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            saved_paths = data.get("paths", [])
            saved_time = data.get("timestamp", 0)

            current_time = time.time()
            state.all_paths = []
            for path in saved_paths:
                path_time = path.get("timestamp", saved_time)
                if current_time - path_time < EXPIRY_SECONDS:
                    state.all_paths.append(path)
    except Exception as e:
        print(f"加载JSON文件失败: {e}")
        state.all_paths = []


def save_paths_to_file():
    """将当前所有路径保存到JSON文件"""
    try:
        data = {
            "timestamp": time.time(),
            "paths": state.all_paths
        }
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存JSON文件失败: {e}")


def clear_room_board(room_id: str):
    """清空指定房间的画板"""
    if room_id not in state.rooms:
        return

    original_count = len(state.all_paths)
    state.all_paths = [p for p in state.all_paths if p.get('room_id') != room_id]

    if original_count != len(state.all_paths):
        save_paths_to_file()
        asyncio.create_task(broadcast_room_board_clear(room_id))


async def broadcast_room_board_clear(room_id: str):
    """广播房间画板清空消息"""
    if room_id not in state.rooms:
        return

    room = state.rooms[room_id]
    message = json.dumps({"type": "board_cleared"})

    for user_id in room["players"]:
        if user_id in state.active_connections:
            try:
                await state.active_connections[user_id].send_text(message)
            except:
                pass


# ==================== 启动加载数据 ====================
load_paths_from_file()
load_banned_devices()


# ==================== Lifespan管理器 ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时执行
    print("🚀 应用程序启动，开始后台清理任务...")
    cleanup_task = asyncio.create_task(cleanup_old_paths())
    game_task = asyncio.create_task(game_loop())

    yield

    # 关闭时执行
    print("📴 应用程序关闭，取消后台任务...")
    cleanup_task.cancel()
    game_task.cancel()
    try:
        await cleanup_task
        await game_task
    except asyncio.CancelledError:
        pass
    save_paths_to_file()
    save_banned_devices()
    print("💾 数据已保存")


# ==================== 创建FastAPI应用 ====================
app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================== 静态文件路由 ====================
@app.get("/tzy.ico")
async def get_favicon():
    """返回自定义图标文件"""
    if os.path.exists("tzy.ico"):
        return FileResponse("tzy.ico", media_type="image/x-icon")
    return HTMLResponse(status_code=404, content="图标文件不存在")


# ==================== 游戏逻辑 ====================
async def game_loop():
    """游戏主循环"""
    while True:
        await asyncio.sleep(1)
        current_time = time.time()

        for room_id, room in list(state.rooms.items()):
            if room["status"] != "playing":
                if room["status"] == "waiting" and len(room["players"]) >= 2:
                    await start_game_auto(room_id)
                continue

            # 检查绘画超时（3分钟）
            if room["draw_start_time"] and current_time - room["draw_start_time"] > 180:
                await next_drawer(room_id, "时间到，无人猜中")

            # 检查是否所有人都猜过了
            if room["guessed_users"] and len(room["guessed_users"]) == len(room["players"]) - 1:  # 减掉画手
                await next_drawer(room_id, "所有人都猜过了")

            # 检查是否有人达到30分
            for player in room["players"].values():
                if player["score"] >= 30:
                    await end_game(room_id, f"玩家 {player['name']} 达到30分获胜")
                    break


async def start_game_auto(room_id: str):
    """自动开始游戏"""
    if room_id not in state.rooms:
        return

    room = state.rooms[room_id]
    if room["status"] != "waiting" or len(room["players"]) < 2:
        return

    # 随机选择第一个画手
    player_ids = list(room["players"].keys())
    first_drawer = random.choice(player_ids)
    room["current_drawer"] = first_drawer
    room["status"] = "playing"
    room["current_word"] = get_random_word()
    room["draw_start_time"] = time.time()
    room["guessed_users"] = []  # 记录已猜对的用户
    room["round_number"] = 1  # 回合数
    room["drawer_order"] = player_ids  # 记录画手顺序

    clear_room_board(room_id)

    for user_id in room["players"]:
        if user_id in state.active_connections:
            try:
                word_to_send = room["current_word"] if user_id == first_drawer else None
                await state.active_connections[user_id].send_text(json.dumps({
                    "type": "game_started",
                    "drawer": first_drawer,
                    "drawer_name": room["players"][first_drawer]["name"],
                    "word": word_to_send,
                    "has_word": user_id == first_drawer,
                    "round_time": 180,
                    "start_time": room["draw_start_time"]
                }))
            except:
                pass


async def next_drawer(room_id: str, reason: str = ""):
    """切换到下一个画手"""
    if room_id not in state.rooms:
        return

    room = state.rooms[room_id]
    if not room["players"]:
        return

    player_ids = list(room["players"].keys())

    # 获取下一个画手（按顺序轮换）
    if "drawer_order" not in room:
        room["drawer_order"] = player_ids.copy()
        random.shuffle(room["drawer_order"])

    current_idx = room["drawer_order"].index(room["current_drawer"]) if room["current_drawer"] in room[
        "drawer_order"] else -1
    next_idx = (current_idx + 1) % len(room["drawer_order"])
    next_drawer = room["drawer_order"][next_idx]

    # 如果下一个画手不在当前玩家中，重新生成顺序
    if next_drawer not in player_ids:
        room["drawer_order"] = player_ids.copy()
        random.shuffle(room["drawer_order"])
        next_drawer = room["drawer_order"][0]

    clear_room_board(room_id)

    new_word = get_random_word()
    room["current_drawer"] = next_drawer
    room["current_word"] = new_word
    room["draw_start_time"] = time.time()
    room["guessed_users"] = []  # 重置已猜用户列表
    room["round_number"] = room.get("round_number", 0) + 1

    for user_id in room["players"]:
        if user_id in state.active_connections:
            try:
                word_to_send = new_word if user_id == next_drawer else None
                await state.active_connections[user_id].send_text(json.dumps({
                    "type": "new_drawer",
                    "drawer": next_drawer,
                    "drawer_name": room["players"][next_drawer]["name"],
                    "word": word_to_send,
                    "has_word": user_id == next_drawer,
                    "reason": reason,
                    "round_time": 180,
                    "start_time": room["draw_start_time"],
                    "round_number": room["round_number"]
                }))
            except:
                pass


async def end_game(room_id: str, reason: str = ""):
    """结束游戏"""
    if room_id not in state.rooms:
        return

    room = state.rooms[room_id]
    room["status"] = "ended"

    players = list(room["players"].values())
    players.sort(key=lambda x: x["score"], reverse=True)
    ranking = [{"name": p["name"], "score": p["score"]} for p in players]

    await broadcast_to_room(room_id, {
        "type": "game_ended",
        "reason": reason,
        "ranking": ranking
    })


async def broadcast_to_room(room_id: str, message: Dict):
    """向房间内所有玩家广播消息"""
    if room_id not in state.rooms:
        return

    room = state.rooms[room_id]
    message_str = json.dumps(message)

    for user_id in room["players"]:
        if user_id in state.active_connections:
            try:
                await state.active_connections[user_id].send_text(message_str)
            except:
                pass


async def broadcast_chat_to_room(room_id: str, message: Dict):
    """向房间内所有玩家广播聊天消息"""
    if room_id not in state.rooms:
        return

    room = state.rooms[room_id]
    if room_id not in state.room_messages:
        state.room_messages[room_id] = []

    state.room_messages[room_id].append({
        **message,
        "timestamp": time.time()
    })

    if len(state.room_messages[room_id]) > 100:
        state.room_messages[room_id] = state.room_messages[room_id][-100:]

    message_str = json.dumps(message)
    for user_id in room["players"]:
        if user_id in state.active_connections:
            try:
                await state.active_connections[user_id].send_text(message_str)
            except:
                pass


# ==================== 后台清理任务 ====================
async def cleanup_old_paths():
    """定期清理超过3分钟无活动的用户及其笔迹"""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        now = time.time()

        inactive_users = []
        for uid, last_time in list(state.user_last_active.items()):
            if now - last_time > EXPIRY_SECONDS:
                inactive_users.append(uid)

        if inactive_users:
            original_count = len(state.all_paths)
            state.all_paths = [p for p in state.all_paths if p['userId'] not in inactive_users]

            for uid in inactive_users:
                if uid in state.user_colors:
                    del state.user_colors[uid]
                if uid in state.user_names:
                    del state.user_names[uid]
                if uid in state.user_last_chat:
                    del state.user_last_chat[uid]
                if uid in state.user_device_map:
                    del state.user_device_map[uid]
                if uid in state.user_paths:
                    del state.user_paths[uid]
                if uid in state.user_rooms:
                    room_id = state.user_rooms[uid]
                    if room_id in state.rooms:
                        if uid in state.rooms[room_id]["players"]:
                            # 广播玩家离开消息
                            asyncio.create_task(broadcast_to_room(room_id, {
                                "type": "player_left",
                                "user_id": uid,
                                "name": state.rooms[room_id]["players"][uid]["name"],
                                "players": [
                                    {"user_id": uid2, "name": p2["name"], "score": p2["score"]}
                                    for uid2, p2 in state.rooms[room_id]["players"].items() if uid2 != uid
                                ]
                            }))
                            del state.rooms[room_id]["players"][uid]
                        if not state.rooms[room_id]["players"]:
                            del state.rooms[room_id]
                    del state.user_rooms[uid]

            for uid in inactive_users:
                if uid in state.active_connections:
                    try:
                        await state.active_connections[uid].close(code=1000, reason="expired")
                    except:
                        pass
                    del state.active_connections[uid]
                if uid in state.user_last_active:
                    del state.user_last_active[uid]

            if original_count != len(state.all_paths):
                save_paths_to_file()
                await broadcast_full_state()
            else:
                await broadcast_online_count()
        else:
            await broadcast_online_count()

        expired_bans = []
        for device_id, ban_info in state.banned_devices.items():
            if ban_info.get("duration") != 0 and now > ban_info["expires_at"]:
                expired_bans.append(device_id)

        for device_id in expired_bans:
            del state.banned_devices[device_id]

        if expired_bans:
            save_banned_devices()


async def broadcast_full_state():
    """广播全量路径及在线人数"""
    if not state.active_connections:
        return
    message = json.dumps({
        "type": "state",
        "paths": state.all_paths,
        "onlineCount": len(state.active_connections)
    })
    disconnected = []
    for uid, ws in state.active_connections.items():
        try:
            await ws.send_text(message)
        except:
            disconnected.append(uid)

    for uid in disconnected:
        cleanup_user(uid)


async def broadcast_online_count():
    """广播当前在线人数"""
    count = len(state.active_connections)
    message = json.dumps({"type": "online", "count": count})
    disconnected = []
    for uid, ws in state.active_connections.items():
        try:
            await ws.send_text(message)
        except:
            disconnected.append(uid)

    for uid in disconnected:
        cleanup_user(uid)


def cleanup_user(user_id: str):
    """清理用户数据"""
    if user_id in state.active_connections:
        del state.active_connections[user_id]
    if user_id in state.user_last_active:
        del state.user_last_active[user_id]
    if user_id in state.user_colors:
        del state.user_colors[user_id]
    if user_id in state.user_names:
        del state.user_names[user_id]
    if user_id in state.user_last_chat:
        del state.user_last_chat[user_id]
    if user_id in state.user_device_map:
        del state.user_device_map[user_id]
    if user_id in state.user_paths:
        del state.user_paths[user_id]
    if user_id in state.user_rooms:
        room_id = state.user_rooms[user_id]
        if room_id in state.rooms:
            if user_id in state.rooms[room_id]["players"]:
                asyncio.create_task(broadcast_to_room(room_id, {
                    "type": "player_left",
                    "user_id": user_id,
                    "name": state.rooms[room_id]["players"][user_id]["name"],
                    "players": [
                        {"user_id": uid, "name": p["name"], "score": p["score"]}
                        for uid, p in state.rooms[room_id]["players"].items() if uid != user_id
                    ]
                }))
                del state.rooms[room_id]["players"][user_id]
            if not state.rooms[room_id]["players"]:
                del state.rooms[room_id]
        del state.user_rooms[user_id]


# ==================== 管理员接口 ====================
@app.get("/admin")
async def admin_page():
    """返回管理员页面"""
    try:
        with open("admin.html", "r", encoding="utf-8") as f:
            html = f.read()
        return HTMLResponse(content=html)
    except FileNotFoundError:
        return HTMLResponse(content="<h1>管理员页面不存在</h1>", status_code=404)


class AdminLogin(BaseModel):
    password: str
    client_id: str


@app.post("/api/admin/login")
async def admin_login(request: Request, login: AdminLogin):
    """管理员登录"""
    client_ip = request.client.host
    client_id = login.client_id

    if client_id in state.admin_attempts:
        attempts = state.admin_attempts[client_id]
        if attempts["count"] >= MAX_LOGIN_ATTEMPTS:
            if time.time() - attempts["last_attempt"] < LOGIN_BLOCK_SECONDS:
                remaining = int(LOGIN_BLOCK_SECONDS - (time.time() - attempts["last_attempt"]))
                return JSONResponse({
                    "success": False,
                    "message": f"尝试次数过多，请等待 {remaining} 秒后重试"
                })
            else:
                del state.admin_attempts[client_id]

    if login.password == ADMIN_PASSWORD:
        if client_id in state.admin_attempts:
            del state.admin_attempts[client_id]
        return JSONResponse({"success": True})
    else:
        if client_id not in state.admin_attempts:
            state.admin_attempts[client_id] = {"count": 0, "last_attempt": 0}

        state.admin_attempts[client_id]["count"] += 1
        state.admin_attempts[client_id]["last_attempt"] = time.time()

        remaining_attempts = MAX_LOGIN_ATTEMPTS - state.admin_attempts[client_id]["count"]

        return JSONResponse({
            "success": False,
            "message": f"密码错误，还剩 {remaining_attempts} 次尝试机会"
        })


@app.get("/api/admin/rooms")
async def get_all_rooms():
    """获取所有房间信息（管理员用）"""
    rooms_data = []
    for room_id, room in state.rooms.items():
        rooms_data.append({
            "room_id": room_id,
            "name": room["name"],
            "hidden": room["hidden"],
            "player_count": len(room["players"]),
            "status": room["status"],
            "players": [
                {
                    "user_id": uid,
                    "name": p["name"],
                    "score": p["score"],
                    "is_drawer": uid == room["current_drawer"],
                    "device_id": state.user_device_map.get(uid, "unknown")
                }
                for uid, p in room["players"].items()
            ],
            "current_word": room["current_word"],
            "messages": state.room_messages.get(room_id, [])
        })

    return JSONResponse(rooms_data)


@app.get("/api/admin/banned")
async def get_banned_list():
    """获取封禁列表"""
    bans = []
    now = time.time()
    for device_id, ban_info in state.banned_devices.items():
        if ban_info.get("duration") == 0 or now < ban_info["expires_at"]:
            remaining = 0 if ban_info.get("duration") == 0 else int(ban_info["expires_at"] - now)
            bans.append({
                "device_id": device_id,
                "reason": ban_info["reason"],
                "banned_at": ban_info["banned_at"],
                "expires_at": ban_info["expires_at"],
                "remaining": remaining,
                "duration": ban_info.get("duration", 0),
                "banned_by": ban_info.get("banned_by", "admin")
            })

    bans.sort(key=lambda x: (0 if x["duration"] == 0 else 1, x["remaining"]))
    return JSONResponse(bans)


class BanRequest(BaseModel):
    device_id: str
    reason: str
    duration: int
    admin: str = "admin"


@app.post("/api/admin/ban")
async def ban_device(ban: BanRequest):
    """封禁设备"""
    expires_at = float('inf') if ban.duration == 0 else time.time() + ban.duration

    state.banned_devices[ban.device_id] = {
        "reason": ban.reason,
        "duration": ban.duration,
        "banned_at": time.time(),
        "expires_at": expires_at,
        "banned_by": ban.admin
    }

    save_banned_devices()

    kicked_users = []
    for user_id, device_id in list(state.user_device_map.items()):
        if device_id == ban.device_id:
            if user_id in state.active_connections:
                try:
                    await state.active_connections[user_id].send_text(json.dumps({
                        "type": "banned",
                        "reason": ban.reason,
                        "duration": ban.duration
                    }))
                    await state.active_connections[user_id].close()
                except:
                    pass
                cleanup_user(user_id)
                kicked_users.append(user_id)

    return JSONResponse({
        "success": True,
        "message": f"设备已封禁，踢出 {len(kicked_users)} 个用户"
    })


class UnbanRequest(BaseModel):
    device_id: str


@app.post("/api/admin/unban")
async def unban_device(unban: UnbanRequest):
    """解封设备"""
    if unban.device_id in state.banned_devices:
        del state.banned_devices[unban.device_id]
        save_banned_devices()
        return JSONResponse({"success": True, "message": "设备已解封"})
    return JSONResponse({"success": False, "message": "设备不存在"})


# ==================== 主页面 ====================
@app.get("/")
async def get_index():
    """返回前端HTML页面"""
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            html = f.read()
        return HTMLResponse(content=html)
    except FileNotFoundError:
        return HTMLResponse(content="<h1>请确保index.html文件存在于同一目录</h1>", status_code=404)


# ==================== WebSocket主连接 ====================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket连接处理"""
    await websocket.accept()
    user_id = str(uuid.uuid4())

    client_ip = websocket.client.host
    user_agent = websocket.headers.get("user-agent", "unknown")
    device_id = hashlib.md5(f"{client_ip}-{user_agent}".encode()).hexdigest()

    if device_id in state.banned_devices:
        ban_info = state.banned_devices[device_id]
        if ban_info.get("duration") == 0 or time.time() < ban_info["expires_at"]:
            remaining = 0 if ban_info.get("duration") == 0 else int(ban_info["expires_at"] - time.time())
            await websocket.send_text(json.dumps({
                "type": "banned",
                "reason": ban_info["reason"],
                "duration": remaining
            }))
            await websocket.close()
            return
        else:
            del state.banned_devices[device_id]
            save_banned_devices()

    state.user_device_map[user_id] = device_id

    user_color = get_random_color()
    user_name = get_random_name()

    state.user_colors[user_id] = user_color
    state.user_names[user_id] = user_name
    state.active_connections[user_id] = websocket
    state.user_last_active[user_id] = time.time()
    state.user_paths[user_id] = []

    try:
        await websocket.send_text(json.dumps({
            "type": "init",
            "userId": user_id,
            "userName": user_name,
            "userColor": user_color,
            "paths": state.all_paths,
            "onlineCount": len(state.active_connections),
            "rooms": [
                {
                    "room_id": rid,
                    "name": r["name"],
                    "player_count": len(r["players"]),
                    "status": r["status"]
                }
                for rid, r in state.rooms.items()
                if not r["hidden"]
            ]
        }))

        await broadcast_online_count()

        while True:
            data = await websocket.receive_text()
            state.user_last_active[user_id] = time.time()

            try:
                msg = json.loads(data)
            except:
                continue

            msg_type = msg.get("type")
            current_room = state.user_rooms.get(user_id)

            if msg_type == "update_name":
                new_name = msg.get("name", "").strip()
                if new_name and len(new_name) <= 20:
                    state.user_names[user_id] = new_name
                    await websocket.send_text(json.dumps({
                        "type": "name_updated",
                        "name": new_name
                    }))

                    if current_room:
                        await broadcast_to_room(current_room, {
                            "type": "player_name_changed",
                            "user_id": user_id,
                            "new_name": new_name
                        })

            elif msg_type == "create_room":
                if user_id in state.user_rooms:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "您已经在房间中"
                    }))
                    continue

                room_name = msg.get("room_name", "").strip()
                if not room_name:
                    room_name = f"{user_name}的房间"

                for room in state.rooms.values():
                    if room["name"] == room_name:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message": "房间名已存在，请使用其他名称"
                        }))
                        break
                else:
                    hidden = msg.get("hidden", False)
                    room_id = generate_room_code()

                    state.rooms[room_id] = {
                        "name": room_name,
                        "hidden": hidden,
                        "players": {
                            user_id: {
                                "name": state.user_names[user_id],
                                "score": 0
                            }
                        },
                        "status": "waiting",
                        "current_drawer": None,
                        "current_word": None,
                        "draw_start_time": None,
                        "guessed_users": [],
                        "drawer_order": []
                    }

                    state.user_rooms[user_id] = room_id

                    await websocket.send_text(json.dumps({
                        "type": "room_created",
                        "room_id": room_id,
                        "room_name": room_name,
                        "hidden": hidden
                    }))

                    await broadcast_room_list()

            elif msg_type == "join_room":
                if user_id in state.user_rooms:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "您已经在房间中"
                    }))
                    continue

                room_id = msg.get("room_id", "").strip()

                if room_id not in state.rooms:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "房间不存在"
                    }))
                    continue

                room = state.rooms[room_id]

                if len(room["players"]) >= 10:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "房间已满"
                    }))
                    continue

                room["players"][user_id] = {
                    "name": state.user_names[user_id],
                    "score": 0
                }
                state.user_rooms[user_id] = room_id

                is_drawer = (room["current_drawer"] == user_id)

                # 如果游戏正在进行，新玩家可以看到当前状态
                if room["status"] == "playing":
                    # 更新画手顺序
                    if user_id not in room["drawer_order"]:
                        room["drawer_order"].append(user_id)

                await websocket.send_text(json.dumps({
                    "type": "room_joined",
                    "room_id": room_id,
                    "room_name": room["name"],
                    "players": [
                        {"user_id": uid, "name": p["name"], "score": p["score"]}
                        for uid, p in room["players"].items()
                    ],
                    "status": room["status"],
                    "current_drawer": room["current_drawer"],
                    "current_word": room["current_word"] if is_drawer else None,
                    "has_word": is_drawer,
                    "messages": state.room_messages.get(room_id, []),
                    "round_time": 180 if room["status"] == "playing" else None,
                    "start_time": room.get("draw_start_time"),
                    "round_number": room.get("round_number", 0)
                }))

                await broadcast_to_room(room_id, {
                    "type": "player_joined",
                    "user_id": user_id,
                    "name": state.user_names[user_id],
                    "players": [
                        {"user_id": uid, "name": p["name"], "score": p["score"]}
                        for uid, p in room["players"].items()
                    ]
                })

                await broadcast_room_list()

            elif msg_type == "draw":
                if not current_room:
                    continue

                room = state.rooms[current_room]

                if room["current_drawer"] != user_id:
                    continue

                if not room["current_word"]:
                    continue

                path = msg.get("path")
                if path and path.get("userId") == user_id:
                    path["timestamp"] = time.time()
                    path["room_id"] = current_room
                    path["path_id"] = len(state.all_paths)
                    state.all_paths.append(path)
                    state.user_paths[user_id].append(len(state.all_paths) - 1)
                    save_paths_to_file()

                    for uid in room["players"]:
                        if uid != user_id and uid in state.active_connections:
                            try:
                                await state.active_connections[uid].send_text(json.dumps({
                                    "type": "draw",
                                    "path": path
                                }))
                            except:
                                pass

            elif msg_type == "undo":
                if not current_room:
                    continue

                room = state.rooms[current_room]

                if room["current_drawer"] != user_id:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "只有画手可以撤销"
                    }))
                    continue

                # 撤销上一步
                if user_id in state.user_paths and state.user_paths[user_id]:
                    last_path_idx = state.user_paths[user_id].pop()
                    if last_path_idx < len(state.all_paths):
                        # 标记删除而不是直接删除（保持索引不变）
                        state.all_paths[last_path_idx] = None
                        state.all_paths = [p for p in state.all_paths if p is not None]
                        save_paths_to_file()

                        # 广播更新
                        await broadcast_full_state()

            elif msg_type == "clear_board":
                if not current_room:
                    continue

                room = state.rooms[current_room]

                if room["current_drawer"] != user_id:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "只有画手可以清空画板"
                    }))
                    continue

                clear_room_board(current_room)

            elif msg_type == "chat":
                if not current_room:
                    continue

                last_chat = state.user_last_chat.get(user_id, 0)
                if time.time() - last_chat < 2:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "消息发送太频繁，请2秒后再试"
                    }))
                    continue

                room = state.rooms[current_room]
                content = msg.get("content", "").strip()

                if not content or len(content) > 100:
                    continue

                state.user_last_chat[user_id] = time.time()

                is_correct = False
                match_percent = 0

                if room["status"] == "playing" and room["current_word"] and room["current_drawer"] != user_id:
                    match_percent = calculate_word_match(content, room["current_word"])

                    # 完全猜中
                    if content == room["current_word"]:
                        if user_id not in room["guessed_users"]:
                            is_correct = True
                            room["guessed_users"].append(user_id)
                            room["players"][user_id]["score"] += 1

                            await broadcast_to_room(current_room, {
                                "type": "correct_guess",
                                "user_id": user_id,
                                "user_name": state.user_names[user_id],
                                "scores": [
                                    {"user_id": uid, "score": p["score"]}
                                    for uid, p in room["players"].items()
                                ]
                            })

                            if room["players"][user_id]["score"] >= 30:
                                await end_game(current_room, f"玩家 {state.user_names[user_id]} 达到30分获胜")
                                continue

                            # 检查是否所有人都猜过了
                            non_drawers = len(room["players"]) - 1
                            if len(room["guessed_users"]) >= non_drawers:
                                await next_drawer(current_room, "所有人都猜中了")
                                continue

                # 广播聊天消息
                await broadcast_chat_to_room(current_room, {
                    "type": "chat",
                    "user_id": user_id,
                    "user_name": state.user_names[user_id],
                    "content": f"某某玩家猜中了！({match_percent}%)" if match_percent > 0 and not is_correct else
                    ("某某玩家猜中了！" if is_correct else content),
                    "is_correct": is_correct,
                    "match_percent": match_percent
                })

            elif msg_type == "leave_room":
                if not current_room:
                    continue

                room_id = current_room
                room = state.rooms[room_id]

                if user_id in room["players"]:
                    player_name = room["players"][user_id]["name"]
                    del room["players"][user_id]

                del state.user_rooms[user_id]

                await websocket.send_text(json.dumps({
                    "type": "room_left"
                }))

                if not room["players"]:
                    del state.rooms[room_id]
                    if room_id in state.room_messages:
                        del state.room_messages[room_id]
                    clear_room_board(room_id)
                else:
                    # 更新画手顺序
                    if "drawer_order" in room and user_id in room["drawer_order"]:
                        room["drawer_order"].remove(user_id)

                    # 如果离开的是画手，切换到下一个
                    if room["current_drawer"] == user_id:
                        await next_drawer(room_id, "画手离开了")

                    # 广播给房间内其他玩家
                    await broadcast_to_room(room_id, {
                        "type": "player_left",
                        "user_id": user_id,
                        "name": player_name,
                        "players": [
                            {"user_id": uid, "name": p["name"], "score": p["score"]}
                            for uid, p in room["players"].items()
                        ]
                    })

                await broadcast_room_list()

            elif msg_type == "get_rooms":
                await websocket.send_text(json.dumps({
                    "type": "room_list",
                    "rooms": [
                        {
                            "room_id": rid,
                            "name": r["name"],
                            "player_count": len(r["players"]),
                            "status": r["status"]
                        }
                        for rid, r in state.rooms.items()
                        if not r["hidden"]
                    ]
                }))

    except WebSocketDisconnect:
        pass
    finally:
        cleanup_user(user_id)
        await broadcast_online_count()
        await broadcast_room_list()


async def broadcast_room_list():
    """广播房间列表给所有在线用户"""
    rooms_data = [
        {
            "room_id": rid,
            "name": r["name"],
            "player_count": len(r["players"]),
            "status": r["status"]
        }
        for rid, r in state.rooms.items()
        if not r["hidden"]
    ]

    message = json.dumps({
        "type": "room_list",
        "rooms": rooms_data
    })

    disconnected = []
    for uid, ws in state.active_connections.items():
        if uid not in state.user_rooms:
            try:
                await ws.send_text(message)
            except:
                disconnected.append(uid)

    for uid in disconnected:
        cleanup_user(uid)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5000)