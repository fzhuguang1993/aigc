"""
gui/dialogs_product.py —— 「产品单选」公共对话框

从 pages_output_lib 里的私有 _pick_product 提出来：成品库、素材库等多个页面
都要「挑一个产品做归属绑定」，以前 pages_material 跨页面引 pages_output_lib
的下划线私有函数（改名即碎、也没承诺复用）。挪到独立模块、去掉下划线，
就成了公开可调用的公共选择器，谁用都会保持稳定。

只依赖数据层 (store.product_store) 与 Qt 控件，不引用任何页面类。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QAbstractItemView,
                               QPushButton)


def pick_product(parent=None):
    """产品单选框：返回 (id, name)，取消返回 None。人工绑定成品/素材归属用。"""
    from store import product_store
    items = product_store.list_products(product_store.TYPE_PRODUCT)
    dlg = QDialog(parent)
    dlg.setWindowTitle("绑定产品")
    dlg.resize(320, 420)
    v = QVBoxLayout(dlg)
    tip = QLabel("选择这条成品归属的产品（人工绑定优先于系统自动识别）：")
    tip.setWordWrap(True)
    v.addWidget(tip)
    lst = QListWidget()
    lst.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    for it in items:
        row = QListWidgetItem(it["name"])
        row.setData(Qt.ItemDataRole.UserRole, int(it["id"]))
        lst.addItem(row)
    if not items:
        lst.addItem("（产品中心还没有产品，请先到「产品中心」新增）")
    v.addWidget(lst, 1)
    bb = QHBoxLayout()
    bb.addStretch(1)
    b_ok = QPushButton("绑定")
    b_ok.setDefault(True)
    b_ok.clicked.connect(dlg.accept)
    b_no = QPushButton("取消")
    b_no.setObjectName("GhostBtn")
    b_no.clicked.connect(dlg.reject)
    bb.addWidget(b_ok)
    bb.addWidget(b_no)
    v.addLayout(bb)
    lst.itemDoubleClicked.connect(lambda _=None, d=dlg: d.accept())
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    cur = lst.currentItem()
    if cur is None or cur.data(Qt.ItemDataRole.UserRole) is None:
        return None
    return int(cur.data(Qt.ItemDataRole.UserRole)), cur.text()
