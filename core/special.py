r"""
core/special.py —— 特供版（测试专用）功能裁剪总入口

给「打一个特别版给别人测试」用：所有特供逻辑与开关集中在本模块，业务代码里只留
极少量、带 `[特供版 special]` 标记的调用钩子。日后把原版文件盖回去（或把下面的
SPECIAL_BUILD 改成 False）即可整体移除，不污染主功能、不改动业务逻辑。

本特供版写死的裁剪（都只作用于「爆款拆解」）：
- 禁用 DeepSeek 语义纠错：勾选即弹窗告知「测试特供版本」并自动取消勾选；
- 成功次数封顶 BREAKDOWN_OK_LIMIT（默认 2）：只有完整成功（非半成品）才计一次，
  失败 / 半成品不计；额度用完后点「开始拆解」直接弹窗拦截、不再进后台调豆包，
  从根上防止反复测试把豆包 / DeepSeek token 耗光。计数落盘 CONFIG_DIR/special.json。

诚实边界：纯本地计数，删掉 special.json 即重置——劝退普通测试者够用，真要硬限制请走服务端。
"""
import json

from core.paths import CONFIG_DIR

# ===================== 特供开关（写死在此） =====================
SPECIAL_BUILD = True          # 改成 False = 恢复原版全部行为，所有钩子变 no-op
BREAKDOWN_OK_LIMIT = 2        # 爆款拆解允许的「成功」次数上限
# ===============================================================

_DISABLED_DS_TIP = ("本测试特供版本不支持 DeepSeek 语义纠错（已强制关闭），"
                    "识别结果直接送豆包拆解。")
_STATE_FILE = CONFIG_DIR / "special.json"


def enabled() -> bool:
    return bool(SPECIAL_BUILD)


# ---------------- 额度计数（落盘） ----------------

def _used() -> int:
    try:
        d = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        return int(d.get("breakdown_ok") or 0)
    except Exception:
        return 0


def _set_used(n: int):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(
            json.dumps({"breakdown_ok": int(n)}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def remaining() -> int:
    """剩余可成功拆解次数；未启用特供版时返回一个极大数示意不限。"""
    if not enabled():
        return 1 << 30
    return max(0, BREAKDOWN_OK_LIMIT - _used())


# ---------------- 钩子①：DeepSeek 强制关闭 ----------------

def hook_disable_deepseek(checkbox):
    """把「✨ DeepSeek 语义纠错」勾选框设为特供版强制关闭：起始取消勾选，用户一旦
    勾上就弹窗告知并回退。未启用特供版时什么都不做（原版行为零变化）。"""
    if not enabled():
        return
    checkbox.setChecked(False)
    checkbox.setToolTip(_DISABLED_DS_TIP)

    def _on_click():
        if checkbox.isChecked():
            checkbox.setChecked(False)                # 先回退再弹窗，避免残留勾选
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(checkbox, "测试特供版本", _DISABLED_DS_TIP)
    # 用 clicked（只响应用户点击）而非 toggled：程序化 setChecked 不触发它，无递归
    checkbox.clicked.connect(_on_click)


# ---------------- 钩子②：爆款拆解额度闸门 ----------------

def guard_breakdown_run(parent) -> bool:
    """点「开始拆解」时的额度闸门。

    返回 False 表示已弹窗拦截，调用方（BreakdownPanel._task）应中止并返回 None；
    未启用特供版直接 True 放行。只在真正要新起一次拆解（非命中缓存短路）时调用。
    """
    if not enabled():
        return True
    if remaining() <= 0:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(
            parent, "测试特供版本",
            f"本测试特供版本的「爆款拆解」最多成功运行 {BREAKDOWN_OK_LIMIT} 次，"
            f"额度已用完，无法再拆解。\n（软件其余功能不受影响）")
        return False
    return True


def note_breakdown_success():
    """一次完整成功（非半成品）后记一次额度；封顶到 BREAKDOWN_OK_LIMIT 不再涨。"""
    if not enabled():
        return
    _set_used(min(_used() + 1, BREAKDOWN_OK_LIMIT))
