"""쇼핑 리스트 앱 (Streamlit) - prd.md 참고."""

import json
import re
import shutil
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import streamlit as st

DATA_FILE = Path(__file__).parent / "shopping_list.json"
# 손상된 파일은 덮어쓰기 전에 이 이름으로 한 번 복사해 둔다.
BACKUP_FILE = DATA_FILE.with_name("shopping_list.corrupt.json")

NAME_MAX = 50
MEMO_MAX = 100
QTY_MIN, QTY_MAX = 1, 999

FILTERS = ["전체", "남은 항목", "완료 항목"]

# 아이템 행: 체크박스 | 이름 | 수량 | 메모 | 수정 | 삭제 (PRD 5장)
ROW_WEIGHTS = [0.5, 4, 1, 3, 0.7, 0.7]

# Streamlit은 폭 640px 미만에서 st.columns를 세로로 쌓는다.
# 보기 모드 아이템 행(st-key-row-*)만 휴대폰에서도 한 줄을 유지하도록 최소한으로 덮어쓴다.
# 체크박스·버튼 열은 내용 폭만 차지하고, 남는 폭을 이름·수량·메모가 나눠 쓴다.
CSS = """
<style>
[class*="st-key-row-"] [data-testid="stHorizontalBlock"] {
    flex-wrap: nowrap;
    gap: 0.4rem;
}
[class*="st-key-row-"] [data-testid="stColumn"] {
    min-width: 0 !important;
}
[class*="st-key-row-"] [data-testid="stColumn"]:is(:nth-child(1), :nth-child(5), :nth-child(6)) {
    flex: 0 0 auto !important;
    width: auto !important;
}
[class*="st-key-row-"] [data-testid="stColumn"] p {
    overflow-wrap: anywhere;
}
[class*="st-key-row-"] .stButton button {
    padding: 0.25rem 0.5rem;
    min-height: 2rem;
}
@media (max-width: 640px) {
    h1 { font-size: 1.9rem !important; }
}
</style>
"""


# ---------- 저장소 ----------

def to_quantity(value) -> int:
    """파일에서 읽은 수량을 QTY_MIN~QTY_MAX 범위의 정수로 바꾼다. 읽을 수 없으면 QTY_MIN."""
    if isinstance(value, bool):
        return QTY_MIN
    try:
        qty = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return QTY_MIN
    return min(max(qty, QTY_MIN), QTY_MAX)


def normalize_item(raw, seen_ids: set[str]) -> dict | None:
    """파일에서 읽은 항목 하나를 PRD 6장 형식으로 맞춘다. 이름이 없으면 None."""
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    # id가 없거나 겹치면 위젯 key가 충돌하므로 새로 만든다.
    item_id = raw.get("id")
    if not isinstance(item_id, str) or not item_id or item_id in seen_ids:
        item_id = uuid.uuid4().hex
    seen_ids.add(item_id)

    memo = raw.get("memo")
    memo = "" if memo is None else str(memo)
    checked = raw.get("checked", False)
    if not isinstance(checked, bool):
        checked = str(checked).strip().lower() in {"true", "1", "yes", "y"}
    created_at = raw.get("created_at")

    return {
        "id": item_id,
        "name": name.strip()[:NAME_MAX],
        "quantity": to_quantity(raw.get("quantity")),
        "memo": memo.strip()[:MEMO_MAX],
        "checked": checked,
        "created_at": created_at if isinstance(created_at, str) else "",
    }


def load_items() -> tuple[list[dict], str | None]:
    """JSON 파일에서 리스트를 읽어온다. (리스트, 상태)를 돌려준다.

    상태: None(정상 또는 파일 없음), "corrupt"(파일 전체를 읽을 수 없음, 빈 리스트),
    "repaired"(일부 항목을 고치거나 건너뜀).
    """
    if not DATA_FILE.exists():
        return [], None
    try:
        with DATA_FILE.open(encoding="utf-8") as f:
            raw_items = json.load(f)
        if not isinstance(raw_items, list):
            raise ValueError("리스트 형식이 아님")
    except (json.JSONDecodeError, ValueError, OSError):
        return [], "corrupt"

    seen_ids: set[str] = set()
    items = []
    for raw in raw_items:
        item = normalize_item(raw, seen_ids)
        if item is not None:
            items.append(item)
    repaired = items != raw_items
    return items, "repaired" if repaired else None


def save_items(items: list[dict]) -> None:
    """리스트를 JSON 파일에 쓴다. 임시 파일에 쓴 뒤 바꿔치기해서 읽는 쪽이 반쯤 쓴 파일을 보지 않게 한다."""
    tmp = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    tmp.replace(DATA_FILE)


@st.cache_resource
def file_lock() -> threading.RLock:
    """모든 세션(탭)이 함께 쓰는 파일 잠금."""
    return threading.RLock()


def file_mtime() -> int | None:
    try:
        return DATA_FILE.stat().st_mtime_ns
    except OSError:
        return None


def sync_from_disk(force: bool = False) -> None:
    """파일이 바뀌었으면(다른 탭에서 저장 등) 세션의 리스트를 파일 내용으로 바꾼다."""
    with file_lock():
        mtime = file_mtime()
        if mtime is None or (not force and mtime == st.session_state["mtime"]):
            return
        items, status = load_items()
        st.session_state["mtime"] = mtime
        if status is not None:
            backed_up = backup_data_file()
            note = f" 원본은 {BACKUP_FILE.name}에 백업했어요." if backed_up else ""
            if status == "corrupt":
                # 읽을 수 없는 파일은 무시하고 세션의 리스트를 유지한다. 다음 저장 때 덮어쓴다.
                st.session_state["load_warning"] = "데이터 파일을 읽을 수 없어 빈 리스트로 시작합니다." + note
                return
            st.session_state["load_warning"] = "데이터 파일의 일부 항목이 잘못되어 고치거나 건너뛰었어요." + note
        st.session_state["items"] = items


def backup_data_file() -> bool:
    """손상된 데이터 파일을 덮어쓰기 전에 복사해 둔다."""
    try:
        shutil.copy2(DATA_FILE, BACKUP_FILE)
        return True
    except OSError:
        return False


def commit() -> None:
    """현재 세션의 리스트를 파일에 저장한다. 실패하면 오류 메시지를 남긴다."""
    try:
        save_items(st.session_state["items"])
        st.session_state["mtime"] = file_mtime()
        st.session_state["load_warning"] = None
    except OSError as e:
        flash("error", f"저장하지 못했어요: {e}")


@contextmanager
def transaction():
    """파일의 최신 내용 위에서 변경하고 저장한다.

    세션마다 따로 들고 있는 리스트로 파일 전체를 덮어쓰면 다른 탭의 변경이 사라지므로,
    변경 직전에 파일을 다시 읽고 잠금을 건 채로 id 기준으로 변경해 저장한다.
    """
    with file_lock():
        sync_from_disk(force=True)
        yield
        commit()


def flash(level: str, text: str) -> None:
    """다음 화면 갱신 때 한 번 보여줄 메시지를 남긴다."""
    st.session_state["flash"] = (level, text)


# ---------- 아이템 조작 ----------

def validate(name: str, qty: int, memo: str) -> tuple[str, str, str | None]:
    """입력을 정리하고 검사한다. (이름, 메모, 오류 메시지 또는 None)을 돌려준다."""
    name = (name or "").strip()
    memo = (memo or "").strip()
    if not name:
        return name, memo, "이름을 입력해 주세요."
    if len(name) > NAME_MAX:
        return name, memo, f"이름은 {NAME_MAX}자까지 입력할 수 있어요."
    if len(memo) > MEMO_MAX:
        return name, memo, f"메모는 {MEMO_MAX}자까지 입력할 수 있어요."
    if not isinstance(qty, int) or not QTY_MIN <= qty <= QTY_MAX:
        return name, memo, f"수량은 {QTY_MIN}~{QTY_MAX} 사이로 입력해 주세요."
    return name, memo, None


def find_item(item_id: str) -> dict | None:
    """id로 아이템을 찾는다."""
    return next((i for i in st.session_state["items"] if i["id"] == item_id), None)


def add_item(name: str, qty: int, memo: str) -> bool:
    """입력을 검사하고 새 아이템을 추가한다. 추가했으면 True."""
    name, memo, error = validate(name, qty, memo)
    if error:
        flash("warning", error)
        return False

    with transaction():
        duplicate = any(i["name"] == name for i in st.session_state["items"])
        st.session_state["items"].append({
            "id": uuid.uuid4().hex,
            "name": name,
            "quantity": qty,
            "memo": memo,
            "checked": False,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })
    if duplicate:
        flash("warning", f"'{name}'은(는) 이미 리스트에 있어요. 그래도 추가했어요.")
    else:
        flash("success", f"'{name}'을(를) 추가했어요.")
    return True


def update_item(item_id: str, name: str, qty: int, memo: str) -> bool:
    """아이템 내용을 고친다. 저장했으면 True."""
    name, memo, error = validate(name, qty, memo)
    if error:
        flash("warning", error)
        return False
    with transaction():
        item = find_item(item_id)
        if item is not None:
            item.update(name=name, quantity=qty, memo=memo)
    if item is None:
        st.session_state["editing_id"] = None
        flash("warning", "다른 곳에서 이미 삭제된 항목이에요.")
        return False
    flash("success", f"'{name}'을(를) 수정했어요.")
    return True


def delete_item(item_id: str) -> None:
    """아이템 하나를 지운다."""
    with transaction():
        item = find_item(item_id)
        if item is not None:
            st.session_state["items"].remove(item)
    if st.session_state["editing_id"] == item_id:
        st.session_state["editing_id"] = None
    if item is not None:
        flash("success", f"'{item['name']}'을(를) 삭제했어요.")


def toggle_item(item_id: str) -> None:
    """체크 상태를 바꾼다.

    다른 탭에서 이미 바꿨을 수 있으므로 뒤집지 않고 체크박스가 가리키는 값으로 맞춘다.
    """
    desired = st.session_state.get(f"check-{item_id}")
    with transaction():
        item = find_item(item_id)
        if item is not None:
            item["checked"] = (not item["checked"]) if desired is None else bool(desired)


def clear_checked() -> None:
    """체크된 아이템을 모두 지운다."""
    with transaction():
        items = st.session_state["items"]
        removed = [i for i in items if i["checked"]]
        st.session_state["items"] = [i for i in items if not i["checked"]]
    if not removed:
        return
    if st.session_state["editing_id"] in {i["id"] for i in removed}:
        st.session_state["editing_id"] = None
    flash("success", f"체크된 항목 {len(removed)}개를 삭제했어요.")


# ---------- 위젯 콜백 ----------

def on_add() -> None:
    add_item(
        st.session_state["add-name"],
        st.session_state["add-qty"],
        st.session_state["add-memo"],
    )


def on_start_edit(item_id: str) -> None:
    st.session_state["editing_id"] = item_id


def on_cancel_edit() -> None:
    st.session_state["editing_id"] = None


def on_save_edit(item_id: str) -> None:
    saved = update_item(
        item_id,
        st.session_state[f"edit-name-{item_id}"],
        st.session_state[f"edit-qty-{item_id}"],
        st.session_state[f"edit-memo-{item_id}"],
    )
    if saved:
        st.session_state["editing_id"] = None


def on_ask_clear() -> None:
    st.session_state["confirm_clear"] = True


def on_confirm_clear() -> None:
    clear_checked()
    st.session_state["confirm_clear"] = False


def on_cancel_clear() -> None:
    st.session_state["confirm_clear"] = False


# ---------- 화면 ----------

def md_escape(text: str) -> str:
    """사용자 입력이 마크다운 문법으로 해석되지 않도록 특수문자를 이스케이프한다."""
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|~<>:$])", r"\\\1", text)


def render_item_row(item: dict) -> None:
    """아이템 한 줄(보기/편집 모드)을 그린다."""
    item_id = item["id"]

    if st.session_state["editing_id"] == item_id:
        with st.container(border=True):
            c_name, c_qty, c_memo = st.columns([4, 1.5, 3])
            c_name.text_input("이름", value=item["name"], max_chars=NAME_MAX,
                              key=f"edit-name-{item_id}")
            c_qty.number_input("수량", min_value=QTY_MIN, max_value=QTY_MAX, step=1,
                               value=item["quantity"], key=f"edit-qty-{item_id}")
            c_memo.text_input("메모", value=item["memo"], max_chars=MEMO_MAX,
                              key=f"edit-memo-{item_id}")
            c_save, c_cancel, _ = st.columns([1, 1, 3])
            c_save.button("저장", key=f"save-{item_id}", type="primary",
                          use_container_width=True,
                          on_click=on_save_edit, args=(item_id,))
            c_cancel.button("취소", key=f"cancel-{item_id}",
                            use_container_width=True, on_click=on_cancel_edit)
        return

    name = md_escape(item["name"])
    memo = md_escape(item["memo"])
    if item["checked"]:
        name = f":gray[~~{name}~~]"
        memo = f":gray[{memo}]" if memo else ""

    c_check, c_name, c_qty, c_memo, c_edit, c_del = st.container(
        key=f"row-{item_id}").columns(ROW_WEIGHTS, vertical_alignment="center")
    # 다른 탭에서 바뀐 체크 상태도 보이도록 위젯 값을 매번 데이터에 맞춘다.
    st.session_state[f"check-{item_id}"] = item["checked"]
    c_check.checkbox(item["name"], key=f"check-{item_id}",
                     on_change=toggle_item, args=(item_id,),
                     label_visibility="collapsed")
    c_name.markdown(name)
    c_qty.markdown(f":gray[x{item['quantity']}]")
    c_memo.caption(memo)
    c_edit.button("✏️", key=f"edit-{item_id}", help="수정",
                  on_click=on_start_edit, args=(item_id,))
    c_del.button("🗑️", key=f"delete-{item_id}", help="삭제",
                 on_click=delete_item, args=(item_id,))


def render_summary(items: list[dict]) -> None:
    """전체/완료/남은 개수와 진행률 바를 그린다."""
    total = len(items)
    done = sum(i["checked"] for i in items)
    ratio = done / total if total else 0.0
    st.markdown(f"전체 **{total}**개 · 완료 **{done}**개 · 남은 **{total - done}**개")
    st.progress(ratio, text=f"{round(ratio * 100)}%")


def render_add_form() -> None:
    """아이템 추가 폼을 그린다."""
    with st.form("add-form", clear_on_submit=True, border=False):
        c_name, c_qty, c_memo, c_btn = st.columns(
            [4, 1.5, 3, 1.2], vertical_alignment="bottom")
        c_name.text_input("이름", max_chars=NAME_MAX, key="add-name",
                          placeholder="예: 우유")
        c_qty.number_input("수량", min_value=QTY_MIN, max_value=QTY_MAX,
                           value=1, step=1, key="add-qty")
        c_memo.text_input("메모", max_chars=MEMO_MAX, key="add-memo",
                          placeholder="선택")
        c_btn.form_submit_button("추가", type="primary",
                                 use_container_width=True, on_click=on_add)


def render_flash() -> None:
    """남겨둔 메시지를 한 번 보여주고 지운다. 성공은 토스트, 나머지는 폼 아래에 표시."""
    message = st.session_state.pop("flash", None)
    if not message:
        return
    level, text = message
    if level == "success":
        st.toast(text, icon="✅")
    else:
        getattr(st, level)(text)


def render_list(items: list[dict]) -> None:
    """필터를 적용하고 체크된 아이템을 아래로 정렬해 목록을 그린다."""
    if not items:
        st.info("리스트가 비어 있어요. 사야 할 물건을 추가해 보세요.")
        return

    choice = st.radio("보기", FILTERS, horizontal=True, key="filter",
                      label_visibility="collapsed")
    if choice == "남은 항목":
        shown = [i for i in items if not i["checked"]]
    elif choice == "완료 항목":
        shown = [i for i in items if i["checked"]]
    else:
        shown = items

    st.divider()
    # sorted는 안정 정렬이라 같은 그룹 안에서는 추가한 순서가 유지된다.
    for item in sorted(shown, key=lambda i: i["checked"]):
        render_item_row(item)
    if not shown:
        st.caption("해당하는 항목이 없어요.")


def render_clear_checked(items: list[dict]) -> None:
    """체크된 항목 일괄 삭제 버튼과 확인 단계를 그린다."""
    if not items:
        return
    st.divider()
    checked_count = sum(i["checked"] for i in items)
    if st.session_state.get("confirm_clear") and checked_count:
        with st.container(border=True):
            st.markdown(f"체크된 항목 **{checked_count}**개를 삭제할까요?")
            c_yes, c_no, _ = st.columns([1, 1, 3])
            c_yes.button("삭제", key="clear-yes", type="primary",
                         use_container_width=True, on_click=on_confirm_clear)
            c_no.button("취소", key="clear-no",
                        use_container_width=True, on_click=on_cancel_clear)
    else:
        _, c_btn = st.columns([3, 2])
        c_btn.button("체크된 항목 모두 삭제", key="clear-checked",
                     use_container_width=True,
                     disabled=checked_count == 0, on_click=on_ask_clear)


def init_state() -> None:
    """세션 상태를 초기화하고, 파일이 바뀌었으면(다른 탭 등) 다시 읽어온다."""
    st.session_state.setdefault("items", [])
    st.session_state.setdefault("mtime", None)
    st.session_state.setdefault("load_warning", None)
    st.session_state.setdefault("editing_id", None)
    st.session_state.setdefault("confirm_clear", False)
    sync_from_disk()


def main() -> None:
    st.set_page_config(page_title="쇼핑 리스트", page_icon="🛒", layout="centered")
    st.markdown(CSS, unsafe_allow_html=True)
    init_state()
    items = st.session_state["items"]

    st.title("🛒 쇼핑 리스트")
    if st.session_state["load_warning"]:
        st.warning(st.session_state["load_warning"])

    render_summary(items)
    st.divider()
    render_add_form()
    render_flash()
    render_list(items)
    render_clear_checked(items)


main()
