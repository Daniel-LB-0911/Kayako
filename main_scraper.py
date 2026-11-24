import asyncio
import os
import re
from contextlib import asynccontextmanager
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
import httpx

load_dotenv()

BASE_STAFF_URL = os.getenv("KAYAKO_BASE_URL", "")
LOGIN_URL = f"{BASE_STAFF_URL}?/Core/Default/Login"
TICKETS_URL = f"{BASE_STAFF_URL}?/Tickets/Manage/Index"
USERNAME = os.getenv("KAYAKO_USERNAME", "")
PASSWORD = os.getenv("KAYAKO_PASSWORD", "")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

session_client: httpx.AsyncClient | None = None
login_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global session_client
    session_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=20.0,
        headers=DEFAULT_HEADERS,
    )
    yield
    await session_client.aclose()


app = FastAPI(title="Kayako Classic Scraper", lifespan=lifespan)


def is_logged_in(html: str) -> bool:
    """判斷目前回傳的頁面是否為已登入狀態"""
    return "Logout" in html and 'type="password"' not in html


async def login_kayako(login_html: str | None = None, page_url: str = LOGIN_URL) -> None:
    """完全模擬瀏覽器 Form data 送出登入"""
    assert session_client is not None

    if not login_html:
        resp = await session_client.get(BASE_STAFF_URL)
        login_html = resp.text
        page_url = str(resp.url)

    soup = BeautifulSoup(login_html, "html.parser")
    pwd_input = soup.find("input", {"type": "password"})
    if not pwd_input:
        raise HTTPException(status_code=500, detail="找不到登入頁面的密碼欄位")

    form = pwd_input.find_parent("form")
    action = form.get("action") if form else None
    post_url = urljoin(page_url, action) if action else LOGIN_URL

    payload: dict[str, str] = {
        "username": USERNAME,
        "password": PASSWORD,
        "submitbutton": "Login",
        "remember": "0",
        "languagecode": "en-us",
        "_ca": "login",
        "_redirectAction": "",
    }

    if form:
        for hidden in form.find_all("input", {"type": "hidden"}):
            name = hidden.get("name")
            if name:
                payload[name] = hidden.get("value", "")

    payload["username"] = USERNAME
    payload["password"] = PASSWORD

    login_resp = await session_client.post(
        post_url,
        data=payload,
        headers={"Referer": page_url},
    )

    if not is_logged_in(login_resp.text):
        raise HTTPException(
            status_code=401,
            detail="Kayako 登入失敗，請確認 .env 中的帳號密碼是否正確",
        )


async def fetch_authenticated_html(url: str) -> str:
    """取得需登入頁面的 HTML，若 Session 過期則加鎖自動登入重試"""
    if not USERNAME or not PASSWORD:
        raise HTTPException(
            status_code=500, detail="請先在 .env 設定 KAYAKO_USERNAME 與 KAYAKO_PASSWORD"
        )

    assert session_client is not None
    resp = await session_client.get(url)
    if is_logged_in(resp.text):
        return resp.text

    async with login_lock:
        # 取得鎖後再次確認，避免多個併發請求重複登入
        resp = await session_client.get(url)
        if is_logged_in(resp.text):
            return resp.text

        await login_kayako(login_html=resp.text, page_url=str(resp.url))
        resp = await session_client.get(url)
        return resp.text


def parse_tickets_from_html(html: str, limit: int = 10) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")

    target_table = None
    col_map: dict[str, int] = {}

    for tr in soup.find_all("tr"):
        raw_cells = tr.find_all(["th", "td"], recursive=False)
        texts = [c.get_text(strip=True) for c in raw_cells]

        if "Subject" in texts and "Ticket ID" in texts:
            target_table = tr.find_parent("table")
            col_idx = 0
            for cell in raw_cells:
                span = int(cell.get("colspan") or 1)
                title = cell.get_text(strip=True)
                if title:
                    col_map[title] = col_idx
                col_idx += span
            break

    if not target_table:
        return []

    results = []
    for row in target_table.find_all("tr"):
        ticket_links = row.find_all(
            "a", href=lambda h: h and "/Tickets/Ticket/View/" in h
        )
        if not ticket_links:
            continue

        cells = row.find_all("td", recursive=False)
        if not cells:
            continue

        def get_col(name: str) -> str:
            idx = col_map.get(name)
            if idx is not None and idx < len(cells):
                return cells[idx].get_text(" ", strip=True)
            return ""

        subject_text = ticket_links[0].get_text(strip=True) or get_col("Subject")
        ticket_url = urljoin(BASE_STAFF_URL, ticket_links[0]["href"])

        results.append({
            "ticket_id": get_col("Ticket ID"),
            "subject": subject_text,
            "owner": get_col("Owner"),
            "last_replier": get_col("Last Replier"),
            "name": get_col("Name"),
            "replies": get_col("Replies"),
            "last_activity": get_col("Last Activity"),
            "reply_due": get_col("Reply Due"),
            "url": ticket_url,
        })

        if len(results) >= limit:
            break

    return results


def clean_multiline_text(text: str) -> str:
    """整理內文空白與多餘換行"""
    lines = [line.strip() for line in text.splitlines()]
    cleaned = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def parse_ticket_detail_html(html: str, ticket_id: str, ticket_url: str) -> dict:
    """解析單張工單的標題、屬性、內部備註與所有對話回覆"""
    soup = BeautifulSoup(html, "html.parser")

    # 1. 抓取工單標題
    subject = ""
    for sel in [".ticketgeneraltitle", ".ticketsubject", "#ticketgeneraltitle", "h2", "h1"]:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            subject = el.get_text(" ", strip=True)
            break

    # 2. 抓取工單屬性（部門、狀態、優先級等，視系統模板而定）
    properties: dict[str, str] = {}
    for prop_box in soup.select(".ticketgeneralpropertiesobject"):
        title_el = prop_box.select_one(".ticketgeneralpropertiestitle")
        val_el = prop_box.select_one(".ticketgeneralpropertiescontent, select, .ticketgeneralpropertiesvalue")
        if title_el:
            key = title_el.get_text(strip=True).rstrip(":")
            if val_el and val_el.name == "select":
                selected = val_el.find("option", selected=True)
                val = selected.get_text(strip=True) if selected else ""
            else:
                val = val_el.get_text(" ", strip=True) if val_el else ""
            if key:
                properties[key] = val

    # 3. 抓取內部備註（Staff Notes）
    notes: list[dict] = []
    for note_el in soup.select(".ticketnotecontainer, .ticketnote"):
        note_text = clean_multiline_text(note_el.get_text("\n", strip=True))
        if note_text:
            notes.append({"content": note_text})

    # 4. 抓取所有工單回覆 / 對話（Kayako Classic 使用 .ticketpostcontainer）
    posts: list[dict] = []
    post_containers = soup.select(".ticketpostcontainer")

    for idx, post_el in enumerate(post_containers, start=1):
        # 發文者名稱
        author_el = post_el.select_one(".ticketpostbarname")
        author = author_el.get_text(" ", strip=True) if author_el else ""

        # 身分標籤（例如 Staff / User / Creator）
        badges = [
            b.get_text(" ", strip=True)
            for b in post_el.select(".ticketpostbarbadge")
            if b.get_text(strip=True)
        ]

        # 發文時間 / 資訊列
        info_el = post_el.select_one(".ticketpostinfo, .ticketpostbarbottom, .ticketpostheader")
        posted_info = info_el.get_text(" ", strip=True) if info_el else ""

        # 附件列表
        attachments = []
        for a in post_el.find_all("a", href=True):
            href = a["href"]
            if "GetAttachment" in href or "/Attachment/" in href:
                attachments.append({
                    "name": a.get_text(" ", strip=True) or "attachment",
                    "url": urljoin(BASE_STAFF_URL, href),
                })

        # 內文解析（優先鎖定內文區塊，並排除側邊欄/資訊列/簽名檔雜訊）
        content_el = post_el.select_one(
            ".ticketpostcontentsdetails, .ticketpostcontent, .ticketpostcontents, .ticketpostdetails, .ticketpostbody"
        )
        target_for_text = content_el if content_el else post_el
        clone = BeautifulSoup(str(target_for_text), "html.parser")

        for noise_sel in [
            ".ticketpostbar",
            ".ticketpostinfo",
            ".ticketpostattachments",
            ".ticketpostbarbadge",
            "script",
            "style",
        ]:
            for noise in clone.select(noise_sel):
                noise.decompose()

        content = clean_multiline_text(clone.get_text("\n", strip=True))

        posts.append({
            "index": idx,
            "author": author,
            "badges": badges,
            "posted_info": posted_info,
            "content": content,
            "attachments": attachments,
        })

    result = {
        "ticket_id": ticket_id,
        "url": ticket_url,
        "subject": subject,
        "properties": properties,
        "notes_count": len(notes),
        "notes": notes,
        "posts_count": len(posts),
        "posts": posts,
    }

    # 若因為系統客製化版型導致抓不到 posts，回傳相關 class 名稱方便除錯
    if not posts:
        found_classes = sorted({
            cls
            for el in soup.find_all(class_=True)
            for cls in el.get("class", [])
            if "ticket" in cls.lower() or "post" in cls.lower()
        })
        result["debug_classes"] = found_classes[:40]

    return result


@app.get("/tickets/latest")
async def get_latest_tickets(limit: int = 10, include_content: bool = False):
    html = await fetch_authenticated_html(TICKETS_URL)
    tickets = parse_tickets_from_html(html, limit=limit)

    if include_content and tickets:
        # 限制同時最多 5 個併發連線，避免對公司主機造成壓力
        sem = asyncio.Semaphore(5)

        async def attach_detail(ticket: dict) -> dict:
            async with sem:
                detail_html = await fetch_authenticated_html(ticket["url"])
                detail = parse_ticket_detail_html(
                    detail_html, ticket["ticket_id"], ticket["url"]
                )
                return {
                    **ticket,
                    "properties": detail["properties"],
                    "notes": detail["notes"],
                    "posts": detail["posts"],
                }

        tickets = await asyncio.gather(*(attach_detail(t) for t in tickets))

    return {"count": len(tickets), "tickets": tickets}


@app.get("/tickets/{ticket_id}")
async def get_ticket_detail(ticket_id: str):
    ticket_url = f"{BASE_STAFF_URL}?/Tickets/Ticket/View/{ticket_id}"
    html = await fetch_authenticated_html(ticket_url)
    return parse_ticket_detail_html(html, ticket_id=ticket_id, ticket_url=ticket_url)