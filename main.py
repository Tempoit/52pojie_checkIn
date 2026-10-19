# -*- coding: utf-8 -*-
"""
吾爱破解（52pojie）自动化签到主程序
支持纯 Python WAF 自动穿透、多账号批量打卡、状态智能判定与多渠道消息推送。
"""

import os
import json
import re
import sys
import time
import random
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
import requests
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None


import http.client
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

import waf_solver

# 复用恩山论坛（ZaiZaiCat-Checkin）的统一通知通道，保持推送方式与模板一致
_ZAIZAI_NOTIFY_DIR = "/ql/data/scripts/ZaiZaiCat-Checkin"
if os.path.isdir(_ZAIZAI_NOTIFY_DIR) and _ZAIZAI_NOTIFY_DIR not in sys.path:
    sys.path.insert(0, _ZAIZAI_NOTIFY_DIR)
try:
    from notification import send_notification, NotificationSound
    _ZAIZAI_NOTIFY = True
except Exception as _e:  # 本地/非青龙环境下退回到自带 notify
    send_notification = None
    NotificationSound = None
    _ZAIZAI_NOTIFY = False
    _ZAIZAI_NOTIFY_ERR = str(_e)

import notify

# ============================================================
# 企业微信专属缩略图管理（用于 52pojie 签到通知卡片）
# 临时素材 3 天过期，故本地缓存 media_id 并到期自动续传
# ============================================================
_QYWX_CFG_PATH = "/ql/data/scripts/ZaiZaiCat-Checkin/config/notification.json"
_MEDIA_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".qywx_media.json")
_MEDIA_IMAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "52pojie_thumb.jpg")
_MEDIA_TTL = 2 * 24 * 3600  # 保守起见 2 天续传（官方 3 天）


def _load_qywx_cfg():
    try:
        with open(_QYWX_CFG_PATH, encoding="utf-8") as f:
            return json.load(f).get("qywx", {})
    except Exception as e:
        logger.warning(f"读取企业微信配置失败: {e}")
        return None


def _get_media_id():
    """返回 52pojie 专属缩略图的 media_id；失效则自动重新上传。失败返回 None。"""
    cfg = _load_qywx_cfg()
    if not cfg:
        return None
    if not os.path.exists(_MEDIA_IMAGE):
        logger.debug("未找到缩略图文件，使用默认样式")
        return None

    # 读缓存
    cache = {}
    if os.path.exists(_MEDIA_CACHE_PATH):
        try:
            with open(_MEDIA_CACHE_PATH, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    now = int(time.time())
    if cache.get("media_id") and now - int(cache.get("ts", 0)) < _MEDIA_TTL:
        return cache["media_id"]

    # 重新上传
    origin = cfg.get("origin", "https://qyapi.weixin.qq.com").rstrip("/")
    try:
        tr = requests.get(
            f"{origin}/cgi-bin/gettoken?corpid={cfg['corpid']}&corpsecret={cfg['corpsecret']}",
            timeout=20,
        ).json()
        token = tr.get("access_token")
        if not token:
            logger.warning(f"获取企业微信 token 失败: {tr.get('errmsg')}")
            return cache.get("media_id")  # 回退到旧值
        with open(_MEDIA_IMAGE, "rb") as f:
            up = requests.post(
                f"{origin}/cgi-bin/media/upload?access_token={token}&type=image",
                files={"media": ("52pojie_thumb.jpg", f, "image/jpeg")},
                timeout=30,
            ).json()
        if up.get("errcode") == 0:
            with open(_MEDIA_CACHE_PATH, "w", encoding="utf-8") as f:
                json.dump({"media_id": up["media_id"], "ts": now}, f)
            logger.info("已更新 52pojie 通知缩略图素材")
            return up["media_id"]
        logger.warning(f"上传缩略图失败: {up.get('errmsg')}")
    except Exception as e:
        logger.warning(f"上传缩略图异常: {e}")
    return cache.get("media_id")


def _send_qywx_mpnews(title: str, content: str) -> bool:
    """用 52pojie 专属缩略图发送企业微信应用消息。失败返回 False。"""
    cfg = _load_qywx_cfg()
    if not cfg:
        return False
    media_id = _get_media_id()
    if not media_id:
        return False
    origin = cfg.get("origin", "https://qyapi.weixin.qq.com").rstrip("/")
    try:
        token = requests.get(
            f"{origin}/cgi-bin/gettoken?corpid={cfg['corpid']}&corpsecret={cfg['corpsecret']}",
            timeout=20,
        ).json().get("access_token")
        if not token:
            return False
        data = {
            "touser": cfg["touser"],
            "msgtype": "mpnews",
            "agentid": cfg["agentid"],
            "mpnews": {"articles": [{
                "title": title,
                "thumb_media_id": media_id,
                "content": content.replace("\n", "<br>"),
                "digest": content,
            }]},
        }
        r = requests.post(
            f"{origin}/cgi-bin/message/send?access_token={token}",
            json=data, timeout=20,
        ).json()
        if r.get("errcode") == 0:
            logger.info("✅ 企业微信应用消息推送成功（52pojie 专属缩略图）")
            return True
        logger.warning(f"企业微信推送失败: {r.get('errmsg')}")
    except Exception as e:
        logger.warning(f"企业微信推送异常: {e}")
    return False

# 日志格式化配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("52PojieCheckIn")

BASE_URL = "https://www.52pojie.cn"
URL_HOME = f"{BASE_URL}/"
URL_APPLY = f"{BASE_URL}/home.php?mod=task&do=apply&id=2&referer=%2F"
URL_DRAW = f"{BASE_URL}/home.php?mod=task&do=draw&id=2"
URL_CREDIT = f"{BASE_URL}/home.php?mod=spacecp&ac=credit"

DEFAULT_HEADERS = {
    'User-Agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    'Accept': "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    'Accept-Language': "zh-CN,zh;q=0.9,en;q=0.8",
    'Accept-Encoding': "gzip, deflate, br",
    'sec-ch-ua': '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    'sec-ch-ua-mobile': "?0",
    'sec-ch-ua-platform': '"Windows"',
    'Sec-Fetch-Dest': "document",
    'Sec-Fetch-Mode': "navigate",
    'Sec-Fetch-Site': "none",
    'Sec-Fetch-User': "?1",
    'Upgrade-Insecure-Requests': "1",
    'Connection': "close",  # 显式关闭连接，防止跨境长连接被 CDN/WAF 掐断导致 RemoteDisconnected
}


def create_resilient_session(cookies_dict: Optional[Dict[str, str]] = None) -> requests.Session:
    """创建具备连接自愈与重试特性的 requests 会话."""
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    if cookies_dict:
        session.cookies.update(cookies_dict)
    adapter = HTTPAdapter(
        max_retries=Retry(total=2, backoff_factor=1, status_forcelist=[502, 503, 504], raise_on_status=False),
        pool_connections=5,
        pool_maxsize=5
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def safe_request(session: requests.Session, method: str, url: str, max_retries: int = 3, retry_on_403: bool = True, **kwargs) -> requests.Response:
    """
    自愈型安全网络请求：
    自动处理连接断开、SSL EOF 与网络抖动，并对 HTTP 403 频控/临时阻断执行套接字重置与退避重试。
    """
    kwargs.setdefault('timeout', 25)
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.request(method, url, **kwargs)
            # 捕获 HTTP 403 Forbidden 频控/硬阻断特征并执行自愈退避
            if resp.status_code == 403 and retry_on_403 and attempt < max_retries:
                wait_sec = 2.5 * attempt + random.uniform(1.0, 2.5)
                logger.warning(f"请求 {url} 遭遇 HTTP 403 Forbidden（可能触发 CDN/WAF 临时频控），释放连接并等待 {wait_sec:.1f} 秒后重试 ({attempt}/{max_retries})...")
                session.close()
                time.sleep(wait_sec)
                continue
            return resp
        except (requests.exceptions.RequestException, http.client.RemoteDisconnected, ConnectionResetError) as e:
            last_err = e
            logger.warning(f"请求 {url} 遭遇网络抖动 ({type(e).__name__}): {e}，正在执行第 {attempt}/{max_retries} 次自愈重试...")
            session.close()  # 释放已失效的底层套接字
            if attempt < max_retries:
                time.sleep(2 * attempt + random.uniform(0.5, 1.5))
    if last_err:
        raise last_err
    return resp


def fetch_user_assets(session: requests.Session) -> Dict[str, str]:
    """查询用户的资产数据（吾爱币、总积分、用户组）."""
    assets = {"cb": "未知", "credit": "未知", "group": "未知"}
    try:
        credit_headers = session.headers.copy()
        credit_headers.update({
            'Referer': URL_HOME,
            'Sec-Fetch-Site': 'same-origin',
        })
        resp = safe_request(session, "GET", URL_CREDIT, headers=credit_headers)
        if waf_solver.is_waf_page(resp.text):
            if waf_solver.bypass_waf(session, URL_CREDIT, resp.text):
                time.sleep(1.5)
                resp = safe_request(session, "GET", URL_CREDIT, headers=credit_headers)
        elif resp.status_code == 403 or "403 Forbidden" in resp.text:
            logger.warning("获取资产数据遭遇 403 阻断，等待冷却后重试...")
            time.sleep(random.uniform(2.5, 4.0))
            resp = safe_request(session, "GET", URL_CREDIT, headers=credit_headers)

        if BeautifulSoup:
            soup = BeautifulSoup(resp.text, 'html.parser')
            for li in soup.find_all('li'):
                t = li.text.strip()
                if '吾爱币' in t:
                    m = re.search(r'(\d+)\s*CB', t)
                    if m:
                        assets["cb"] = f"{m.group(1)} CB"
                elif '积分:' in t and '总积分' in t:
                    m = re.search(r'积分:\s*(\d+)', t)
                    if m:
                        assets["credit"] = m.group(1)
            g_elem = soup.find('a', id='g_upmine')
            if g_elem:
                assets["group"] = g_elem.text.replace('用户组:', '').strip()

        # 正则降级保障
        if assets["cb"] == "未知":
            m_cb = re.search(r'吾爱币:\s*.*?(\d+)\s*CB', resp.text, re.S)
            if m_cb:
                assets["cb"] = f"{m_cb.group(1)} CB"
        if assets["credit"] == "未知":
            m_c = re.search(r'积分:\s*</em>(\d+)', resp.text) or re.search(r'积分:\s*(\d+)', resp.text)
            if m_c:
                assets["credit"] = m_c.group(1)
        if assets["group"] == "未知":
            m_g = re.search(r'用户组:\s*.*?target="_blank">([^<]+)</a>', resp.text) or re.search(r'用户组:\s*([^\s<|]+)', resp.text)
            if m_g:
                assets["group"] = m_g.group(1).strip()
    except Exception as e:
        logger.warning(f"获取资产数据异常: {e}")
    return assets



def parse_cookies(cookie_str: str) -> Dict[str, str]:
    """解析 Cookie 字符串为字典."""
    cookies = {}
    for item in cookie_str.split(';'):
        item = item.strip()
        if not item:
            continue
        parts = item.split('=', 1)
        if len(parts) == 2:
            cookies[parts[0].strip()] = parts[1].strip()
    return cookies


def check_cookie_validity(cookies: Dict[str, str]) -> Tuple[bool, str]:
    """检查 Cookie 中是否包含 Discuz 核心认证字段."""
    has_saltkey = any(k.endswith("_saltkey") for k in cookies.keys())
    has_auth = any(k.endswith("_auth") for k in cookies.keys())
    if not (has_saltkey and has_auth):
        return False, "缺少核心凭据 (需包含 *_saltkey 与 *_auth)"
    return True, "Cookie 字段完整"


def parse_account_username(html: str) -> str:
    """尝试从论坛页面提取当前登录的用户名或 UID."""
    if not html or "<title>403 Forbidden</title>" in html:
        return ""
    if BeautifulSoup:
        soup = BeautifulSoup(html, "html.parser")
        user_elem = soup.find("strong", class_="vwmy") or soup.find("a", title="访问我的空间")
        if user_elem:
            return user_elem.text.strip()
    else:
        m = re.search(r'<strong class="vwmy"[^>]*><a[^>]*>([^<]+)</a></strong>', html) or \
            re.search(r'<a[^>]*title="访问我的空间"[^>]*>([^<]+)</a>', html)
        if m:
            return m.group(1).strip()

    # 查找个人中心链接
    match = re.search(r"home\.php\?mod=space&uid=(\d+)", html)
    if match:
        return f"UID_{match.group(1)}"

    # 校验是否为论坛正常 HTML 页面（包含基础论坛关键词）
    if "吾爱破解" in html or "52pojie" in html or "discuz" in html.lower():
        return "已登录用户"
    return ""


def mask_username(username: str) -> str:
    """对账号名称进行安全脱敏，避免公开日志泄露真实账号/QQ号."""
    if not username or username in ("未知", "已登录用户"):
        return username
    # 如果是 UID_xxxx 形式
    if username.startswith("UID_") and len(username) > 6:
        return f"UID_{username[4:6]}****{username[-2:]}"
    if len(username) <= 2:
        return username[0] + "*"
    elif len(username) <= 4:
        return username[0] + "**" + username[-1]
    else:
        # 保留前2位和后2位，中间用固定星号遮盖（如 qq670607946 -> qq6****46）
        return f"{username[:3]}****{username[-2:]}"


def check_in_account(account_index: int, cookie_str: str) -> Dict[str, str]:
    """执行单个账号的签到流程."""
    cookie_str = cookie_str.strip()
    if not cookie_str:
        return {"index": str(account_index), "user": "未知", "status": "失败", "msg": "Cookie 为空"}

    cookies_dict = parse_cookies(cookie_str)
    valid, msg = check_cookie_validity(cookies_dict)
    if not valid:
        return {"index": str(account_index), "user": "未知", "status": "失败", "msg": msg}

    session = create_resilient_session(cookies_dict)

    logger.info(f"===> 开始处理第 {account_index} 个账号...")

    username = "未知"

    try:
        # 1. 访问首页进行预检
        logger.info(f"[{account_index}] 正在访问论坛首页进行登录与签到预检...")
        home_headers = session.headers.copy()
        home_headers.update({'Sec-Fetch-Site': 'none'})
        home_resp = safe_request(session, "GET", URL_HOME, headers=home_headers)

        # 检查首页是否遇到 WAF 挑战盾
        if waf_solver.is_waf_page(home_resp.text):
            logger.info(f"[{account_index}] 首页触发 WAF 挑战盾，正在求解穿透...")
            if waf_solver.bypass_waf(session, URL_HOME, home_resp.text):
                time.sleep(2.0)
                home_resp = safe_request(session, "GET", URL_HOME, headers=home_headers)
            else:
                return {"index": str(account_index), "user": "未知", "status": "失败", "msg": "首页 WAF 穿透失败"}

        # 检查首页是否遭遇 403 阻断
        if home_resp.status_code == 403 or "403 Forbidden" in home_resp.text:
            logger.warning(f"[{account_index}] 首页遭遇 403 Forbidden 阻断，尝试自愈冷却重连...")
            session.close()
            time.sleep(random.uniform(3.5, 6.0))
            home_resp = safe_request(session, "GET", f"{URL_HOME}?_={int(time.time()*1000)}", headers=home_headers)
            if waf_solver.is_waf_page(home_resp.text):
                waf_solver.bypass_waf(session, URL_HOME, home_resp.text)
                time.sleep(2.0)
                home_resp = safe_request(session, "GET", URL_HOME, headers=home_headers)

        if home_resp.status_code == 403 or "403 Forbidden" in home_resp.text:
            return {"index": str(account_index), "user": "未知", "status": "失败", "msg": "论坛首页遭遇 403 访问限制 (WAF/CDN阻断)"}

        # 检查是否已登录
        if "您需要先登录才能继续本操作" in home_resp.text or ("登录" in home_resp.text and "退出" not in home_resp.text):
            if "member.php?mod=logging&action=login" in home_resp.text:
                return {"index": str(account_index), "user": "未知", "status": "失败", "msg": "Cookie 已失效，请重新获取"}

        username = parse_account_username(home_resp.text) or "已登录用户"
        logger.info(f"[{account_index}] 账号身份确认: {mask_username(username)}")

        status = "成功"
        msg = ""

        if "wbs.png" in home_resp.text:
            logger.info(f"[{account_index}] 首页状态显示：今日已打卡 (wbs.png)")
            status = "成功"
            msg = "今日已签到 (首页状态确认)"
        else:
            # 预留充分缓冲间隔，模拟人工点击节奏，规避高频阻断
            wait_time = random.uniform(2.5, 4.5)
            logger.info(f"[{account_index}] 预检通过，等待 {wait_time:.1f} 秒后发起任务申请...")
            time.sleep(wait_time)

            # 2. 申请任务 (Apply)
            logger.info(f"[{account_index}] 正在申请签到任务 (id=2)...")
            apply_headers = session.headers.copy()
            apply_headers.update({
                'Referer': URL_HOME,
                'Sec-Fetch-Site': 'same-origin',
            })
            apply_resp = safe_request(session, "GET", URL_APPLY, headers=apply_headers)

            # 任务申请页面若遇到 WAF 盾，自动穿透
            if waf_solver.is_waf_page(apply_resp.text):
                logger.info(f"[{account_index}] 任务申请接口触发 WAF 挑战盾，正在求解穿透...")
                if waf_solver.bypass_waf(session, URL_APPLY, apply_resp.text):
                    time.sleep(2.0)
                    logger.info(f"[{account_index}] WAF 穿透成功，重新请求任务申请...")
                    apply_resp = safe_request(session, "GET", URL_APPLY, headers=apply_headers)
                else:
                    return {"index": str(account_index), "user": username, "status": "失败", "msg": "任务申请接口 WAF 穿透失败", "cb": "未知", "credit": "未知", "group": "未知"}
            elif apply_resp.status_code == 403 or "403 Forbidden" in apply_resp.text:
                logger.warning(f"[{account_index}] 任务申请接口遭遇 403 阻断，尝试重新从首页握手刷新 WAF 通行证...")
                session.close()
                time.sleep(random.uniform(3.5, 5.5))
                home_refresh = safe_request(session, "GET", URL_HOME, headers=home_headers)
                if waf_solver.is_waf_page(home_refresh.text):
                    waf_solver.bypass_waf(session, URL_HOME, home_refresh.text)
                    time.sleep(2.0)
                apply_resp = safe_request(session, "GET", URL_APPLY, headers=apply_headers)

            # 申请与领奖之间留出从容缓冲间隔（3.0 ~ 5.5 秒）
            draw_wait = random.uniform(3.0, 5.5)
            logger.info(f"[{account_index}] 任务申请完毕，等待 {draw_wait:.1f} 秒后领取奖励...")
            time.sleep(draw_wait)

            # 3. 领取签到奖励 (Draw)
            logger.info(f"[{account_index}] 正在领取签到任务奖励...")
            draw_headers = session.headers.copy()
            draw_headers.update({
                'Referer': URL_APPLY,
                'Sec-Fetch-Site': 'same-origin',
            })
            draw_resp = safe_request(session, "GET", URL_DRAW, headers=draw_headers)

            if waf_solver.is_waf_page(draw_resp.text):
                logger.info(f"[{account_index}] 领奖接口触发 WAF 挑战盾，正在求解穿透...")
                if waf_solver.bypass_waf(session, URL_DRAW, draw_resp.text):
                    time.sleep(2.0)
                    draw_resp = safe_request(session, "GET", URL_DRAW, headers=draw_headers)
                else:
                    return {"index": str(account_index), "user": username, "status": "失败", "msg": "领奖接口 WAF 穿透失败", "cb": "未知", "credit": "未知", "group": "未知"}
            elif draw_resp.status_code == 403 or "403 Forbidden" in draw_resp.text:
                logger.warning(f"[{account_index}] 领奖接口遭遇 403 阻断，尝试冷却重试...")
                session.close()
                time.sleep(random.uniform(4.0, 6.0))
                draw_resp = safe_request(session, "GET", URL_DRAW, headers=draw_headers)

            # 4. 解析结果反馈
            result_text = ""
            if BeautifulSoup:
                soup = BeautifulSoup(draw_resp.text, "html.parser")
                msg_elem = soup.find("div", id="messagetext")
                if msg_elem and msg_elem.find("p"):
                    result_text = msg_elem.find("p").text.strip()
            else:
                m = re.search(r'<div id="messagetext"[^>]*>.*?<p>([^<]+)</p>', draw_resp.text, re.S)
                if m:
                    result_text = m.group(1).strip()

            if not result_text:
                if draw_resp.status_code == 403 or "403 Forbidden" in draw_resp.text:
                    status, msg = "失败", "接口触发 403 访问限制 (WAF/CDN 频控阻断)"
                elif "您需要先登录才能继续本操作" in draw_resp.text:
                    status, msg = "失败", "Cookie 已失效"
                elif "不是进行中的任务" in draw_resp.text or "今日已签到" in draw_resp.text:
                    status, msg = "成功", "今日已签到"
                elif "恭喜" in draw_resp.text or "奖励" in draw_resp.text:
                    status, msg = "成功", "签到成功并领取奖励"
                else:
                    clean_msg = re.sub(r'<[^>]+>', ' ', draw_resp.text).strip()
                    clean_msg = ' '.join(clean_msg.split())[:60]
                    status, msg = "未知", f"未获取到明确消息: {clean_msg}"
            else:
                logger.info(f"[{account_index}] 领奖反馈: {result_text}")
                if "403 Forbidden" in result_text:
                    status, msg = "失败", "接口触发 403 访问限制 (WAF/CDN 频控阻断)"
                elif "您需要先登录才能继续本操作" in result_text:
                    status, msg = "失败", "Cookie 已失效"
                elif "恭喜" in result_text or "完成" in result_text or "奖励" in result_text:
                    status, msg = "成功", f"签到成功 ({result_text})"
                elif "不是进行中的任务" in result_text or "已经完成" in result_text or "今日已签到" in result_text:
                    status, msg = "成功", "今日已签到"
                else:
                    status, msg = "提示", result_text

        # 5. 抓取用户资产（吾爱币、总积分、用户组）
        assets = fetch_user_assets(session)
        logger.info(f"[{account_index}] 资产总览: 吾爱币={assets['cb']}, 积分={assets['credit']}, 用户组={assets['group']}")

        return {
            "index": str(account_index),
            "user": username,
            "status": status,
            "msg": msg,
            "cb": assets["cb"],
            "credit": assets["credit"],
            "group": assets["group"]
        }


    except requests.exceptions.RequestException as e:
        logger.error(f"[{account_index}] 网络请求异常: {e}")
        return {"index": str(account_index), "user": username, "status": "失败", "msg": f"网络异常: {e}"}
    except Exception as e:
        logger.error(f"[{account_index}] 处理过程发生错误: {e}")
        return {"index": str(account_index), "user": username, "status": "失败", "msg": f"异常: {e}"}


def build_report_text(results_list: List[Dict[str, str]], time_str: str,
                      mask: bool = False, duration: int = 0) -> str:
    """生成与恩山论坛签到一致风格的汇报卡片，支持公开日志脱敏."""
    total = len(results_list)
    ok_count = sum(1 for r in results_list if r.get("status") == "成功")
    fail_count = total - ok_count

    lines = ["📊 执行统计:"]
    if ok_count > 0:
        lines.append(f"✅ 成功: {ok_count} 个账号")
    if fail_count > 0:
        lines.append(f"❌ 失败: {fail_count} 个账号")
    lines.append(f"📈 总计: {total} 个账号")
    lines.append("")
    lines.append("📝 详情:")

    for r in results_list:
        display_user = mask_username(r.get("user", "未知账号")) if mask else r.get("user", "未知账号")
        group = r.get("group") or "未知"
        if group == "未知":
            group = "会员"

        if r.get("status") == "成功":
            if "已签到" in r.get("msg", "") or "已打卡" in r.get("msg", ""):
                msg = "今日已签到"
            else:
                msg = "签到成功"
            cb = r.get("cb") or "未知"
            credit = r.get("credit") or "未知"
            if cb != "未知":
                msg = f"{msg}，吾爱币 {cb}"
            if credit != "未知":
                msg += f"｜积分 {credit}"
            lines.append(f"  ✅ [{display_user}·{group}] {msg}")
        else:
            err = str(r.get("msg", "未知错误"))
            if len(err) > 50:
                err = err[:50] + "..."
            lines.append(f"  ❌ [{display_user}·{group}] {err}")

    lines.append("")
    if duration > 0:
        lines.append(f"⏱️ 执行耗时: {int(duration)}秒")
    lines.append(f"🕐 完成时间: {time_str}")
    return "\n".join(lines)


def build_title(results_list: List[Dict[str, str]]) -> str:
    """根据结果生成通知标题（成功/失败/部分成功）。"""
    total = len(results_list)
    ok_count = sum(1 for r in results_list if r.get("status") == "成功")
    fail_count = total - ok_count
    if total == 0:
        return "吾爱破解签到异常 ❌"
    if fail_count == 0:
        return "吾爱破解签到成功 ✅"
    if ok_count == 0:
        return "吾爱破解签到失败 ❌"
    return "吾爱破解签到部分成功 ⚠️"


def main():
    _start_time = datetime.now()
    logger.info("==========================================")
    logger.info("   吾爱破解（52pojie）自动签到任务启动   ")
    logger.info("==========================================")

    # 优先从环境变量获取 Cookie
    raw_cookies = os.environ.get("POJIE52_COOKIE") or os.environ.get("PJ52_COOKIE")

    # 如果环境变量未设置，检查是否存在本地私有测试文件 (不会提交到 git)
    if not raw_cookies and os.path.exists("local_cookie.txt"):
        try:
            with open("local_cookie.txt", "r", encoding="utf-8") as f:
                raw_cookies = f.read().strip()
                logger.info("从本地 local_cookie.txt 文件载入 Cookie。")
        except Exception:
            pass

    if not raw_cookies:
        warning_msg = (
            "未检测到环境变量 POJIE52_COOKIE 或 PJ52_COOKIE！\n"
            "请前往 GitHub 仓库 -> Settings -> Secrets and variables -> Actions\n"
            "新增 Repository secret: POJIE52_COOKIE，填入吾爱破解账号 Cookie 即可。"
        )
        logger.warning(warning_msg)
        notify.send("吾爱破解签到提醒", warning_msg)
        # 本地调试或首次配置时友好退出
        sys.exit(0)

    # 支持多账号（换行符、@@ 或 & 分隔）
    account_list = []
    for line in re.split(r'[\r\n]+|@@|&', raw_cookies):
        line = line.strip()
        if line and not line.startswith("#"):
            account_list.append(line)

    logger.info(f"共检测到 {len(account_list)} 个账号待处理。")

    results = []
    for idx, cookie in enumerate(account_list, 1):
        res = check_in_account(idx, cookie)
        results.append(res)
        if idx < len(account_list):
            sleep_time = random.uniform(2.0, 5.0)
            logger.info(f"等待 {sleep_time:.1f} 秒后处理下一个账号...")
            time.sleep(sleep_time)

    # 格式化最终汇报 (北京时间 UTC+8)
    from datetime import timezone
    tz_beijing = timezone(timedelta(hours=8))
    beijing_time = datetime.now(tz_beijing).strftime("%Y-%m-%d %H:%M:%S")

    # 计算耗时
    _duration = int((datetime.now() - _start_time).total_seconds())

    # 生成私有推送内容（真实账号）与公开展示内容（脱敏账号）
    private_summary = build_report_text(results, beijing_time, mask=False, duration=_duration)
    public_summary = build_report_text(results, beijing_time, mask=True, duration=_duration)

    # 控制台日志仅输出脱敏版本，彻底杜绝开源后云端控制台泄露账号
    logger.info("\n" + public_summary)

    # 通知标题与提示音（与恩山签到保持一致的分级方式）
    title = build_title(results)
    if title.endswith("✅"):
        sound = NotificationSound.BIRDSONG if _ZAIZAI_NOTIFY else None
    elif title.endswith("⚠️"):
        sound = NotificationSound.BELL if _ZAIZAI_NOTIFY else None
    else:
        sound = NotificationSound.ALARM if _ZAIZAI_NOTIFY else None

    # 优先用 52pojie 专属缩略图走企业微信；失败再回退到恩山通道
    if _send_qywx_mpnews(title, private_summary):
        pass
    elif _ZAIZAI_NOTIFY:
        # 走恩山同一条推送通道（企业微信应用消息等）
        try:
            send_notification(title=title, content=private_summary, sound=sound)
            logger.info("✅ 吾爱破解签到推送已通过恩山通知通道发送")
        except Exception as e:
            logger.error(f"❌ 恩山通知通道发送失败: {e}")
            notify.send(title, private_summary, public_content=public_summary)
    else:
        logger.warning("未找到恩山通知模块(%s)，使用脚本自带推送", _ZAIZAI_NOTIFY_ERR)
        notify.send(title, private_summary, public_content=public_summary)

    logger.info("所有签到流程处理完毕。")

    # 若有任何账号未签到成功，退出码返回 1，以便 GitHub Actions 清晰展示失败状态
    has_failed = any(r["status"] != "成功" for r in results)
    if has_failed:
        logger.error("检测到存在签到失败的账号，退出码设为 1。")
        sys.exit(1)


if __name__ == "__main__":
    main()
