#!/usr/bin/env python3
"""comcbt.com 리눅스마스터 2급(r2) 게시판 공개 첨부파일 수집기.

실행 전: python -m pip install requests beautifulsoup4
실행:   python comcbt_r2_downloader.py

사이트 이용약관과 저작물 이용 조건을 확인하고, 개인적·허용된 범위에서 사용하세요.
과도한 트래픽을 유발하지 않도록 순차적으로 요청합니다.
"""

import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ===== 사용자 설정 =====
BOARD_URL = "https://www.comcbt.com/xe/r2"
MAX_PAGES = 4                 # 확인 당시 총 4페이지. 이후 늘어나면 숫자를 변경하세요.
SAVE_DIR = Path.home() / "Downloads" / "comcbt_r2"
ONLY_EXTENSIONS = None       # 전체: None / PDF만: {".pdf"} / HWP만: {".hwp"}
REQUEST_DELAY_SECONDS = 1.5  # 서버 부담을 줄이기 위해 요청 사이에 대기
# =====================


HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PersonalAttachmentDownloader/1.0)",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
}


def safe_name(name: str, max_length: int = 110) -> str:
    """윈도우/맥/리눅스에서 사용할 수 있는 안전한 파일명."""
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:max_length].rstrip(" .") or "이름없음"


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_html(session: requests.Session, url: str) -> BeautifulSoup:
    response = session.get(url, timeout=(15, 60))
    response.raise_for_status()
    if "html" not in response.headers.get("Content-Type", "").lower():
        raise ValueError(f"HTML 페이지가 아닙니다: {url}")
    return BeautifulSoup(response.content, "html.parser")


def post_id_from_url(url: str):
    parsed = urlparse(url)
    if parsed.netloc not in {"www.comcbt.com", "comcbt.com"}:
        return None
    match = re.fullmatch(r"/xe/r2/(\d+)/?", parsed.path)
    if match:
        return match.group(1)
    # XE 환경의 일부 목록 스킨은 querystring 링크를 사용합니다.
    query = parse_qs(parsed.query)
    if query.get("mid") == ["r2"] and query.get("document_srl", [""])[0].isdigit():
        return query["document_srl"][0]
    return None


def list_posts(session: requests.Session):
    all_posts = {}
    for page in range(1, MAX_PAGES + 1):
        url = f"https://www.comcbt.com/xe/index.php?mid=r2&page={page}"
        print(f"[목록] {page}/{MAX_PAGES}페이지 확인")
        try:
            soup = fetch_html(session, url)
        except (requests.RequestException, ValueError) as error:
            print(f"  목록 접근 실패: {error}")
            continue
        added = 0
        for anchor in soup.select("a[href]"):
            link = urljoin(url, anchor["href"])
            post_id = post_id_from_url(link)
            if post_id is None or post_id in all_posts:
                continue
            title = anchor.get_text(" ", strip=True)
            if not title:
                continue
            # /xe/r2/숫자 형태로 통일해서 쿼리 파라미터 중복 제거
            all_posts[post_id] = (f"{BOARD_URL}/{post_id}", title)
            added += 1
        print(f"  게시글 {added}개 추가 (누계 {len(all_posts)}개)")
        time.sleep(REQUEST_DELAY_SECONDS)
    return all_posts


def list_attachments(soup: BeautifulSoup, post_url: str):
    found = {}
    for anchor in soup.select("a[href]"):
        link = urljoin(post_url, anchor["href"])
        parsed = urlparse(link)
        query = parse_qs(parsed.query)
        if query.get("act") != ["procFileDownload"]:
            continue
        if parsed.netloc not in {"www.comcbt.com", "comcbt.com"}:
            continue
        file_id = query.get("file_srl", [None])[0]
        if not file_id or not file_id.isdigit() or file_id in found:
            continue
        file_name = safe_name(anchor.get_text(" ", strip=True) or f"file_{file_id}")
        suffix = Path(file_name).suffix.lower()
        if ONLY_EXTENSIONS is not None and suffix not in ONLY_EXTENSIONS:
            continue
        found[file_id] = (link, file_name)
    return found


def save_attachment(session, post_url, link, filename, file_id, folder):
    # 같은 이름의 파일도 서로 덮어쓰지 않도록 첨부파일 고유 번호를 붙입니다.
    item = Path(filename)
    if item.suffix:
        filename = f"{safe_name(item.stem, 95)}__{file_id}{item.suffix}"
    else:
        filename = f"{safe_name(filename, 95)}__{file_id}"
    dest = folder / filename
    if dest.is_file() and dest.stat().st_size > 0:
        return "skip"

    part = dest.with_name(dest.name + ".part")
    try:
        with session.get(
            link,
            headers={"Referer": post_url},
            stream=True,
            timeout=(15, 120),
            allow_redirects=True,
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" in content_type:
                raise ValueError("파일 대신 HTML 응답을 받았습니다 (로그인/접근 제한 가능성)")
            with part.open("wb") as handle:
                first_chunk = True
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    if first_chunk and chunk.lstrip().lower().startswith((b"<!doctype html", b"<html")):
                        raise ValueError("파일 대신 HTML 페이지를 받았습니다")
                    first_chunk = False
                    handle.write(chunk)
        if not part.exists() or part.stat().st_size == 0:
            raise ValueError("0바이트 파일을 받았습니다")
        part.replace(dest)
        return "ok"
    except (requests.RequestException, OSError, ValueError) as error:
        print(f"    실패: {filename} ({error})")
        part.unlink(missing_ok=True)
        return "failed"


def main():
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    session = make_session()
    posts = list_posts(session)
    if not posts:
        print("게시글을 찾지 못했습니다. 사이트 구조 또는 접근 제한을 확인하세요.")
        return

    counts = {"ok": 0, "skip": 0, "failed": 0}
    files_found = 0
    for index, (post_id, (post_url, title)) in enumerate(posts.items(), start=1):
        print(f"\n[게시글 {index}/{len(posts)}] {title}")
        try:
            soup = fetch_html(session, post_url)
            attachments = list_attachments(soup, post_url)
        except (requests.RequestException, ValueError) as error:
            print(f"  게시글 접근 실패: {error}")
            counts["failed"] += 1
            time.sleep(REQUEST_DELAY_SECONDS)
            continue
        time.sleep(REQUEST_DELAY_SECONDS)
        if not attachments:
            print("  첨부파일 없음")
            continue

        folder = SAVE_DIR / safe_name(f"{post_id}_{title}", max_length=85)
        folder.mkdir(parents=True, exist_ok=True)
        files_found += len(attachments)
        for file_id, (link, filename) in attachments.items():
            result = save_attachment(session, post_url, link, filename, file_id, folder)
            counts[result] += 1
            print(f"  [{'완료' if result == 'ok' else '기존 파일' if result == 'skip' else '실패'}] {filename}")
            if result != "skip":
                time.sleep(REQUEST_DELAY_SECONDS)

    print("\n========== 작업 결과 ==========")
    print(f"확인한 게시글: {len(posts)}개")
    print(f"발견한 첨부파일: {files_found}개")
    print(f"새로 다운로드: {counts['ok']}개")
    print(f"기존 파일 건너뜀: {counts['skip']}개")
    print(f"실패: {counts['failed']}개")
    print(f"저장 위치: {SAVE_DIR}")
    if counts['failed']:
        print("실패한 경우 재실행하면 기존 파일은 건너뛰고 다시 시도합니다.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n사용자가 중단했습니다. 다시 실행하면 기존 파일은 건너뜁니다.")
