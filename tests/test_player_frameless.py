"""
tests/test_player_frameless.py —— 无边框播放器：画面顶到窗边、全屏真全屏、
全屏播放条是压在画面上的半透明浮层（静止收起 / 悬停不透明 / 位置可设）

钉住用户报的两条：
1) 「播放器边框很大」——以前画面外面套了一层 #PlayerShell，四周留 10×8 黑边
   + 1px 描边 + 10px 圆角遮罩。现在窗口只有三行（窗控条 / 画面 / 播放条占位），
   四周 0 边距，画面宽度必须等于窗口宽度，窗口也不再剪遮罩。
2) 「现在不是真全屏」——全屏时那一圈壳子照旧在。现在进全屏收掉顶部窗控条，
   窗口尺寸要盖住整块屏（含任务栏那一条的高度）。

顺带钉住画面上的「点 vs 拖」：无边框后画面是唯一能抓着挪窗的地方，
所以按住拖走不该顺手把播放/暂停也切一下（同思维导图那套判据）。

浮层那几条钉的是新形态：播放条不进布局（靠一条 spacer 占位），全屏时那行让给
画面、栏本体摆到 _bar_rect 上压在画面上；位置只有一个——屏幕底部居中，
进度条恒在最下面一行横着（贴左右边的那两档已按用户要求去掉）；鼠标一动就回来、
真静止才收起；悬停不透明、平时半透明。

还钉两条用户报的尺寸问题：画面缩放必须跟着窗口走（fitInView 是一次性矩阵，
不重跑就成了“视频尺寸跟播放器尺寸不符”），以及播放区域/窗宽的底线（小过一个
手机屏幕、窄过播放条的需求都不行）。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 早于任何 PySide6 导入

import pytest                     # noqa: E402
from PySide6.QtCore import QElapsedTimer, QPoint, QRect, QSize, QSizeF, Qt   # noqa: E402
from PySide6.QtTest import QTest                         # noqa: E402
from PySide6.QtWidgets import QApplication               # noqa: E402
from gui.widgets import VideoPlayerDialog                # noqa: E402

NOMOD = Qt.KeyboardModifier.NoModifier


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _player(qapp, tmp_path, w=900, h=600):
    """开一个播放器并摆好尺寸（文件只建空壳，后端能不能解出画面不影响这几条断言）"""
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"")
    dlg = VideoPlayerDialog(None, str(f))
    dlg.resize(w, h)
    dlg.show()
    qapp.processEvents()
    return dlg


def _wait(qapp, ms):
    """让事件循环真空转这么久：QTimer 走的是墙钟，光 processEvents 不会把时间吹走"""
    clk = QElapsedTimer()
    clk.start()
    while clk.elapsed() < ms:
        qapp.processEvents()


def _spot(dlg):
    """画面中间那一点（事件打在 view 的视口上，坐标就是视口坐标）"""
    vp = dlg.view.viewport()
    return QPoint(vp.width() // 2, vp.height() // 2)


def test_picture_touches_the_window_edges(qapp, tmp_path):
    """画面四周不再有边框：0 边距、画面宽 == 窗口宽、窗口不再剪圆角遮罩"""
    dlg = _player(qapp, tmp_path)
    m = dlg.layout().contentsMargins()
    assert (m.left(), m.top(), m.right(), m.bottom()) == (0, 0, 0, 0)
    assert dlg.view.x() == 0 and dlg.view.width() == dlg.width(), \
        "画面要顶到窗口左右两边：%d×%d @ x=%d，窗宽 %d" % (
            dlg.view.x(), dlg.view.width(), dlg.view.width(), dlg.width())
    assert dlg.mask().isEmpty(), "不再用 setMask 剪圆角（那也是一圈“边框”）"
    # 画面自己是一块 QGraphicsView（视频走 QPainter 合成，浮层才透得出来），
    # 且外面没有再套一层带描边的壳子
    assert dlg.view.frameShape() == dlg.view.Shape.NoFrame
    assert dlg.layout().count() == 3, \
        "顶层只有三行（窗控条 / 画面 / 播放条占位），播放条本体不在布局里：%d" % (
            dlg.layout().count())
    dlg.deleteLater()


def test_deck_is_a_manual_overlay_not_a_layout_row(qapp, tmp_path):
    """非全屏：播放条贴在窗口底，占位由 spacer 顶出来（两者必须严丝合缝）"""
    dlg = _player(qapp, tmp_path)
    assert dlg._deck.layout() is not None
    assert dlg._deck.parent() is dlg, "播放条是窗口的直接子控件，靠手动摆位"
    _wait(qapp, 120)        # 后端报错（提示行会长一截 spacer）是从另一个线程回来的
    assert dlg._deck_gap.sizeHint().height() == dlg._deck_h, \
        "占位行高与播放条实测高不一致：%d vs %d" % (
            dlg._deck_gap.sizeHint().height(), dlg._deck_h)
    assert dlg._deck.geometry().bottom() == dlg.height() - 1, \
        "播放条要贴窗口下沿：bar=%d win=%d" % (dlg._deck.geometry().bottom(),
                                            dlg.height())
    assert dlg._deck.y() == dlg.view.geometry().bottom() + 1, \
        "画面与播放条之间不能留白，也不能叠在一起：bar.y=%d view.bottom=%d" % (
            dlg._deck.y(), dlg.view.geometry().bottom())
    assert dlg._deck_alpha == 255, "非全屏是实体栏，不玩半透明"
    dlg.deleteLater()


def test_fullscreen_covers_the_whole_screen(qapp, tmp_path):
    """真全屏：盖住整块屏（含任务栏那一条），并且把顶部窗控条收掉"""
    dlg = _player(qapp, tmp_path)
    dlg._set_fullscreen(True)
    qapp.processEvents()
    scr = dlg.screen()
    assert bool(dlg.windowState() & Qt.WindowState.WindowFullScreen), "没进全屏状态"
    assert not dlg._topbar.isVisible(), "全屏不该还顶着一条窗控条"
    assert dlg.height() >= scr.availableGeometry().height(), \
        "全屏只到可用区高度（任务栏那一条还露着）：%d < %d" % (
            dlg.height(), scr.availableGeometry().height())
    assert dlg.width() >= scr.geometry().width()
    # 播放条那一行整个让给画面：画面要吃满整块屏
    assert dlg._deck_gap.sizeHint().height() == 0, "全屏没把播放条那行让给画面"
    assert dlg.view.geometry().height() >= dlg.height(), "画面没铺满全屏"
    dlg._set_fullscreen(False)
    qapp.processEvents()
    assert dlg._topbar.isVisible(), "退出全屏窗控条要回来"
    assert dlg._deck_gap.sizeHint().height() == dlg._deck_h, \
        "退出全屏要把那一行高度还回来（不然画面底下会少一条播放条）"
    dlg.deleteLater()


def test_fullscreen_bar_floats_over_the_picture(qapp, tmp_path):
    """全屏时播放条改浮层：摆在 _bar_rect 上、压在画面里（不再占一行）"""
    dlg = _player(qapp, tmp_path)
    dlg._set_fullscreen(True)
    qapp.processEvents()
    g = dlg._deck.geometry()
    assert g == dlg._bar_rect(), \
        "浮层没落在算出来的那块矩形上：%s vs %s" % (g, dlg._bar_rect())
    assert dlg.view.geometry().contains(g), "浮层没压在画面上：%s" % (g,)
    assert g.top() < dlg.height() - 1, "浮层还贴着窗口下沿＝没浮起来"
    assert dlg._deck.isVisible(), "刚进全屏浮层该在身上"
    assert dlg.btn_min_fs.isVisible() and dlg.btn_close_fs.isVisible(), \
        "顶栏收掉了，浮层末尾得能最小化 / 关窗"
    dlg._set_fullscreen(False)
    qapp.processEvents()
    assert not dlg.btn_min_fs.isVisible() and not dlg.btn_close_fs.isVisible(), \
        "非全屏顶栏就在，浮层那两颗窗控该收掉"
    dlg.deleteLater()


def test_bar_always_sits_at_the_screen_bottom(qapp, tmp_path):
    """全屏浮层只有一档：屏幕底部居中，横版竖版都一样

    以前有下方/左侧/右侧/自动四档，贴左右边时进度条落在屏幕中部，
    用户口径是“不管什么情况进度条都在下面横着”，所以侧边那两档整体删掉：
    这里钉住“旧设置值残留也不会把栏带回侧边”（ui_state 里可能还写着 left/right）。"""
    dlg = _player(qapp, tmp_path)
    assert not hasattr(dlg, "_bar_side"), "位置选项已取消，不该还留着读数的那一手"
    assert not hasattr(dlg, "BAR_SIDES"), "同上（设置页也不再依赖这个常量）"
    for size in ((1920, 1080), (1080, 1920)):
        dlg._last_size = size
        dlg._set_fullscreen(True)
        qapp.processEvents()
        g = dlg._deck.geometry()
        assert g == dlg._bar_rect(), "浮层没落在底部居中那块矩形上：%s" % (g,)
        assert abs(g.center().x() - dlg.width() // 2) <= 2, "浮层要左右居中：%s" % (g,)
        assert dlg.height() - g.bottom() - 1 == dlg.GAP, "该离屏幕底一个 GAP：%s" % (g,)
        assert dlg.slider.orientation() == Qt.Orientation.Horizontal, "进度条必须横着"
        dlg._set_fullscreen(False)
        qapp.processEvents()
    dlg.deleteLater()


def test_seek_row_is_the_bottom_row(qapp, tmp_path):
    """进度条那一行（播放/快退/步长/进度/时间）得排在栏的最下面"""
    dlg = _player(qapp, tmp_path)
    top = dlg._rows.indexOf(dlg._row_top)
    seek = dlg._rows.indexOf(dlg._row_seek)
    assert top < seek, "进度行要放在最下面一行（上排是倍速/音量/窗控）"
    assert dlg.slider.parent() is dlg._deck and dlg._row_seek.indexOf(dlg.slider) >= 0
    dlg.deleteLater()


def test_picture_scale_follows_the_window(qapp, tmp_path):
    """窗口一变小画面必须重铺：fitInView 设的是一次性缩放矩阵，不重跑
    画面就停在旧尺寸上（用户报的“视频尺寸不符合播放器尺寸”）"""
    dlg = _player(qapp, tmp_path)
    dlg.item.setSize(QSizeF(1080, 1920))
    dlg.resize(700, 760)
    qapp.processEvents()
    dlg._refit()                      # 按大窗铺好
    qapp.processEvents()

    def spills_out():
        vis = dlg.view.mapToScene(dlg.view.viewport().rect()).boundingRect()
        return not vis.contains(dlg.item.boundingRect())

    assert not spills_out(), "刚铺完画面就该整幅在内"
    dlg.resize(440, 480)
    qapp.processEvents()
    assert not spills_out(), \
        "缩小窗口后画面没跟着重铺（缩放停在 %d×%d 那一次的尺寸上）" % (
            dlg.view.viewport().width(), dlg.view.viewport().height())
    dlg.deleteLater()


def test_picture_has_a_phone_sized_floor(qapp, tmp_path):
    """播放区域下限＝iPhone 17 屏幕（402×874），方向跟着视频转；
    窗宽底线还要顶住播放条的需求（不然栏里的按钮被压扁）"""
    dlg = _player(qapp, tmp_path)
    dlg._avail = lambda: QRect(0, 0, 2560, 1440)   # 按用户的 2K 屏算，别被离屏 800 顶住
    dlg._fit_to_video(QSize(1080, 1920))
    qapp.processEvents()
    assert (dlg.view.minimumWidth(), dlg.view.minimumHeight()) \
        == (dlg.PHONE_SHORT, dlg.PHONE_LONG), "竖版视频的画面下限要是手机尺寸"
    assert dlg.minimumWidth() >= dlg._deck.minimumSizeHint().width(), \
        "窗宽底线没顶住播放条的需求：%d < %d" % (
            dlg.minimumWidth(), dlg._deck.minimumSizeHint().width())
    assert dlg.height() >= dlg.PHONE_LONG, "整窗高度至少装得下一个手机屏幕"
    dlg._fit_to_video(QSize(1920, 1080))
    qapp.processEvents()
    assert (dlg.view.minimumWidth(), dlg.view.minimumHeight()) \
        == (dlg.PHONE_LONG, dlg.PHONE_SHORT), "横版视频要把这一对方过来"
    dlg.deleteLater()


def test_default_window_never_squeezes_the_controls(qapp, tmp_path):
    """开窗尺寸必须宽过播放条的需求：用户报的“按钮挤压了”就是这一条

    播放条已从布局里拿出来了，再没人替它顶窗宽；栏里的按钮、下拉、时间标签
    各自都有 minimumSizeHint，窗宽不够时布局只能把它们压到比自己底线还窄。"""
    dlg = _player(qapp, tmp_path)
    dlg._avail = lambda: QRect(0, 0, 2560, 1440)
    dlg._scale_factor = lambda: 1.0      # 按设置页 100% 那档算（用户存过自己的百分比）
    for vid in (QSize(1080, 1920), QSize(720, 1280), QSize(1920, 1080)):
        dlg._fitted = None               # 逐条重算（同一比例会被 REFIT_EPS 拦住）
        dlg._fit_to_video(vid)
        _wait(qapp, 60)
        need = dlg._deck.minimumSizeHint().width()
        assert dlg.width() >= need, "窗宽不够摆下播放条：%d < %d（%dx%d 视频）" % (
            dlg.width(), need, vid.width(), vid.height())
        assert dlg._deck.width() >= need, "栏本体被窗宽压扁：%d < %d" % (
            dlg._deck.width(), need)
        for w in dlg._row1_widgets:
            assert w.width() >= w.minimumSizeHint().width(), \
                "%s 在 %dx%d 窗里被压到 %d（底线 %d）" % (
                    type(w).__name__, dlg.width(), dlg.height(),
                    w.width(), w.minimumSizeHint().width())
    dlg.deleteLater()


def test_bar_idle_hide_and_wake(qapp, tmp_path):
    """鼠标真静止到点才收起浮层；画面里随便动一下鼠标就回来

    不再要求“动到栏那条边缘”才算唤回：用户反馈过往栏那边动的时候它收得特别快，
    那会儿计时根本没归零。"""
    dlg = _player(qapp, tmp_path)
    dlg._set_fullscreen(True)
    qapp.processEvents()
    dlg._bar_hold = False
    dlg._hover_bar = lambda: False      # 真光标位置不算数（离屏跑测试时不归我们管）
    dlg.IDLE_MS = 100
    dlg._wake_bar()
    assert not dlg._deck.isHidden(), "唤回后浮层该在身上"
    _wait(qapp, 320)
    assert dlg._deck.isHidden(), "鼠标静止过了 IDLE_MS，浮层没收起"
    # 画面左上角（离底部那条远远的地方）动一下鼠标就该回来
    QTest.mouseMove(dlg.view.viewport(), QPoint(4, 4))
    qapp.processEvents()
    assert not dlg._deck.isHidden(), "画面里动了鼠标没把浮层唤回来"
    # 鼠标一直比 IDLE_MS 动得勤就不能收：隔 40ms 动一下，连做三轮
    for i in range(3):
        _wait(qapp, 40)
        QTest.mouseMove(dlg.view.viewport(), QPoint(8 + i, 6))
        qapp.processEvents()
    assert not dlg._deck.isHidden(), "鼠标一直在动，浮层不该收"
    dlg._set_fullscreen(False)
    qapp.processEvents()
    assert dlg._deck.isVisible(), "退出全屏浮层必须回到身上（不然没播放条了）"
    dlg.deleteLater()


def test_hover_bar_is_opaque_idle_is_translucent(qapp, tmp_path):
    """悬停在浮层上＝不透明，移开＝回到半透明（能透出视频）"""
    dlg = _player(qapp, tmp_path)
    dlg._set_fullscreen(True)
    qapp.processEvents()
    dlg._bar_hold = False
    dlg._hover_bar = lambda: True
    dlg._update_bar_state()
    assert dlg._deck_alpha == dlg.ALPHA_HOT == 255, "鼠标在栏里该变不透明"
    dlg._hover_bar = lambda: False
    dlg._update_bar_state()
    assert dlg._deck_alpha == dlg.ALPHA_IDLE == 150, "移开该回到半透明"
    # 报错时不许收起：顶栏在全屏是收着的，栏再一收用户就看不到“为什么没画面”
    dlg._bar_hold = True
    dlg._update_bar_state()
    assert dlg._deck_alpha == dlg.ALPHA_HOT, "报错钉住时该保持不透明"
    dlg._set_fullscreen(False)
    dlg._bar_hold = False
    dlg._update_bar_state()
    assert dlg._deck_alpha == 255, "非全屏永远是实体栏"
    dlg.deleteLater()


def test_click_picture_toggles_play(qapp, tmp_path):
    """原地按一下松开＝点：切播放/暂停"""
    dlg = _player(qapp, tmp_path)
    hits = []
    dlg._toggle_play = lambda: hits.append(1)
    spot = _spot(dlg)
    vp = dlg.view.viewport()
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, NOMOD, spot)
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, NOMOD, spot)
    qapp.processEvents()
    assert hits == [1], "单击画面没走到播放/暂停"
    dlg.deleteLater()


def test_dragging_picture_moves_instead_of_toggling(qapp, tmp_path):
    """按住画面拖出去＝挪窗，不能顺带把播放态切一下"""
    dlg = _player(qapp, tmp_path)
    hits = []
    dlg._toggle_play = lambda: hits.append(1)
    spot = _spot(dlg)
    vp = dlg.view.viewport()
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, NOMOD, spot)
    QTest.mouseMove(vp, spot + QPoint(80, 50))
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, NOMOD, spot + QPoint(80, 50))
    qapp.processEvents()
    assert hits == [], "拖拽挪窗被当成了点击（播放态被拨了一下）"
    assert dlg._press is None and dlg._drag_pos is None, "按下状态没清干净"
    dlg.deleteLater()


def test_double_click_picture_fullscreens_without_pausing(qapp, tmp_path):
    """双击画面＝进全屏，且不会顺手把片停在暂停（Qt6 没有 detail()，走 mouseDoubleClickEvent）"""
    dlg = _player(qapp, tmp_path)
    hits = []
    dlg._toggle_play = lambda: hits.append(1)
    spot = _spot(dlg)
    vp = dlg.view.viewport()
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, NOMOD, spot)
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, NOMOD, spot)      # 第一下
    QTest.mouseDClick(vp, Qt.MouseButton.LeftButton, NOMOD, spot)       # 第二下＝双击
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, NOMOD, spot)
    qapp.processEvents()
    assert dlg._fullscreen, "双击画面没进全屏"
    assert len(hits) == 2, "单击拨了一下、双击又拨回来：应该是两下"
    dlg.deleteLater()
