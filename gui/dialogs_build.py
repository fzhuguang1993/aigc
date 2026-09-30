"""
gui/dialogs_build.py —— 批量基建的两个编辑对话框（账户 / 计划方案）

三级组织化后，批量基建的搭建逻辑已迁到独立页 gui/pages_local_build.py
（LocalBuildPage）；本模块只保留被该页复用的两个对话框：
- LocalAccountDialog：本地推账户新增/编辑（凭证按姓名加密落 SQLite，
  额外承载「归属执照」与「负责成员」——层级/逐个混合授权）；
- PlanDialog：计划方案模板编辑（参数 + 素材清单，明文存 config）。
账户凭证是使用者自有的开放平台授权（OAuth token），加密口径与一键发布同；
本模块不套维护人口令。
"""
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                               QLineEdit, QComboBox, QDoubleSpinBox, QDialog,
                               QMessageBox, QFormLayout)

from gui.tool_panels import FileListWidget


def _split_list(s):
    """逗号/空格分隔 → 去空列表（标题、地域等共用）。"""
    for sep in ("，", ",", " "):
        s = s.replace(sep, "\n")
    return [t for t in (x.strip() for x in s.split("\n")) if t]


# ====================================================================
# 账户编辑对话框（使用者自有的开放平台授权，不套维护人口令）
# ====================================================================
class LocalAccountDialog(QDialog):
    """本地推账户新增/编辑。三级组织化后额外承载「归属执照」与「负责成员」：
    licenses=[{id,name}] 供下拉、default_license_id 预置（从执照页下钻进入时带上），
    org_on 决定「负责成员」下拉是否出现（单机不启用组织则隐藏），
    owners=[成员名] 为可选项；owner 留空＝继承所属执照/客户（就近继承）。"""

    def __init__(self, parent=None, account=None, licenses=None,
                 owners=None, org_on=True, default_license_id=0):
        super().__init__(parent)
        self._edit = account
        self._org_on = bool(org_on)
        self.setWindowTitle("编辑本地推账户" if account else "添加本地推账户")
        self.resize(520, 340)
        v = QVBoxLayout(self)
        tip = QLabel("凭证为巨量引擎开放平台「巨量营销」应用 OAuth2.0 授权取得的 "
                     "access_token（本地推业务），以使用人姓名加密存本机，"
                     "不明文落盘、不上传任何服务器。")
        tip.setWordWrap(True)
        tip.setObjectName("PageTip")
        v.addWidget(tip)

        form = QFormLayout()
        self.ed_label = QLineEdit()
        self.ed_label.setPlaceholderText("账户别名，如：门店A-主户 / 小号B")
        self.ed_advertiser = QLineEdit()
        self.ed_advertiser.setPlaceholderText("本地推广告主账户 ID（纯数字，接口调用全程携带）")
        self.ed_at = QLineEdit()
        self.ed_at.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_rt = QLineEdit()
        self.ed_rt.setEchoMode(QLineEdit.EchoMode.Password)
        # 归属执照下拉：可空（未分组），从执照页下钻时默认落在当前执照
        self.cb_license = QComboBox()
        self.cb_license.addItem("（未分组）", 0)
        for lic in (licenses or []):
            self.cb_license.addItem(str(lic.get("name") or ""),
                                    int(lic.get("id") or 0))
        form.addRow("别名：", self.ed_label)
        form.addRow("账户ID：", self.ed_advertiser)
        form.addRow("归属执照：", self.cb_license)
        form.addRow("Access Token：", self.ed_at)
        form.addRow("Refresh Token：", self.ed_rt)
        # 负责成员下拉：仅启用组织时出现，留空＝继承上级（层级/逐个混合授权）
        self.cb_owner = None
        if self._org_on:
            self.cb_owner = QComboBox()
            self.cb_owner.addItem("（继承所属执照/客户）", "")
            for nm in (owners or []):
                self.cb_owner.addItem(nm, nm)
            form.addRow("负责成员：", self.cb_owner)
        v.addLayout(form)

        bb = QHBoxLayout()
        b_ok = QPushButton("💾 保存")
        b_ok.clicked.connect(self._accept)
        b_no = QPushButton("取消")
        b_no.setObjectName("GhostBtn")
        b_no.clicked.connect(self.reject)
        bb.addStretch(1)
        bb.addWidget(b_ok)
        bb.addWidget(b_no)
        v.addLayout(bb)

        if account:
            self.ed_label.setText(account.label or "")
            self.ed_advertiser.setText(account.advertiser_id or "")
            secret = account.secret or {}
            self.ed_at.setText(secret.get("access_token", ""))
            self.ed_rt.setText(secret.get("refresh_token", ""))
            lid = int(getattr(account, "license_id", 0) or 0)
            i = self.cb_license.findData(lid)
            if i >= 0:
                self.cb_license.setCurrentIndex(i)
            if self.cb_owner is not None:
                o = getattr(account, "owner", "") or ""
                j = self.cb_owner.findData(o)
                if j >= 0:
                    self.cb_owner.setCurrentIndex(j)
        else:
            i = self.cb_license.findData(int(default_license_id or 0))
            if i >= 0:
                self.cb_license.setCurrentIndex(i)

    def _accept(self):
        label = self.ed_label.text().strip()
        if not label:
            QMessageBox.information(self, "提示", "请填写账户别名")
            return
        advertiser = self.ed_advertiser.text().strip()
        if not advertiser:
            QMessageBox.information(self, "提示", "请填写本地推账户 ID（advertiser_id）")
            return
        at = self.ed_at.text().strip()
        if not at:
            QMessageBox.information(self, "提示", "请填写 Access Token")
            return
        from video_text_tools.local_push.models import LocalAccount
        self._result = LocalAccount(
            id=(self._edit.id if self._edit else ""),
            platform=(self._edit.platform if self._edit else "douyin"),
            label=label, advertiser_id=advertiser,
            secret={"access_token": at, "refresh_token": self.ed_rt.text().strip()},
            extra=(self._edit.extra if self._edit else {}),
            license_id=int(self.cb_license.currentData() or 0),
            owner=(self.cb_owner.currentData() if self.cb_owner else "") or "")
        self.accept()

    def result_account(self):
        return getattr(self, "_result", None)


# ====================================================================
# 计划方案编辑对话框
# ====================================================================
class PlanDialog(QDialog):
    def __init__(self, parent=None, plan=None):
        super().__init__(parent)
        self._edit = plan
        self.setWindowTitle("编辑计划方案" if plan else "新建计划方案")
        self.resize(640, 620)
        v = QVBoxLayout(self)
        tip = QLabel("一个方案 = 项目/营销层参数 + 素材清单。执行时按「方案 × 账户」"
                     "批量搭建：素材上传 → 创建项目 → 创建营销。")
        tip.setWordWrap(True)
        tip.setObjectName("PageTip")
        v.addWidget(tip)

        from video_text_tools.local_push.models import (PROMO_TYPES, GOALS,
                                                        PROMO_STORE, GOAL_LEAD)
        form = QFormLayout()
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("方案名，如：门店拉新-30元")
        self.cb_promo = QComboBox()
        for key, name in PROMO_TYPES:
            self.cb_promo.addItem(name, key)
        self.cb_goal = QComboBox()
        for key, name in GOALS:
            self.cb_goal.addItem(name, key)
        self.sp_budget = QDoubleSpinBox()
        self.sp_budget.setRange(0, 1000000)
        self.sp_budget.setDecimals(2)
        self.sp_budget.setSuffix(" 元/天")
        self.sp_bid = QDoubleSpinBox()
        self.sp_bid.setRange(0, 100000)
        self.sp_bid.setDecimals(2)
        self.sp_bid.setSuffix(" 元")
        self.ed_region = QLineEdit()
        self.ed_region.setPlaceholderText("投放地域，逗号分隔（地名或编码），如：杭州市,苏州市")
        self.cb_age = QComboBox()
        for key, name in (("all", "不限"), ("18-23", "18-23"), ("24-30", "24-30"),
                          ("31-40", "31-40"), ("41-49", "41-49"), ("50+", "50+")):
            self.cb_age.addItem(name, key)
        self.cb_gender = QComboBox()
        for key, name in (("all", "不限"), ("male", "男"), ("female", "女")):
            self.cb_gender.addItem(name, key)
        self.ed_hours = QLineEdit()
        self.ed_hours.setPlaceholderText("投放时段，如 9-22（空=全时段）")
        self.ed_target = QLineEdit()
        self.ed_target.setPlaceholderText("门店 ID / 商品 ID（按推广类型填对应那个）")
        self.ed_uid = QLineEdit()
        self.ed_uid.setPlaceholderText("投放抖音号（可空）")
        self.ed_titles = QLineEdit()
        self.ed_titles.setPlaceholderText("素材标题，逗号分隔（可空）")
        self.ed_remark = QLineEdit()
        form.addRow("方案名：", self.ed_name)
        form.addRow("推广类型：", self.cb_promo)
        form.addRow("营销目标：", self.cb_goal)
        form.addRow("日预算：", self.sp_budget)
        form.addRow("出价：", self.sp_bid)
        form.addRow("投放地域：", self.ed_region)
        form.addRow("年龄：", self.cb_age)
        form.addRow("性别：", self.cb_gender)
        form.addRow("投放时段：", self.ed_hours)
        form.addRow("门店/商品ID：", self.ed_target)
        form.addRow("投放抖音号：", self.ed_uid)
        form.addRow("素材标题：", self.ed_titles)
        form.addRow("备注：", self.ed_remark)
        v.addLayout(form)

        self.files = FileListWidget("素材视频（至少 1 条）")
        v.addWidget(self.files)

        bb = QHBoxLayout()
        b_ok = QPushButton("💾 保存")
        b_ok.clicked.connect(self._accept)
        b_no = QPushButton("取消")
        b_no.setObjectName("GhostBtn")
        b_no.clicked.connect(self.reject)
        bb.addStretch(1)
        bb.addWidget(b_ok)
        bb.addWidget(b_no)
        v.addLayout(bb)

        if plan:
            self._load_plan(plan)
        else:
            self.cb_promo.setCurrentIndex(
                max(self.cb_promo.findData(PROMO_STORE), 0))
            self.cb_goal.setCurrentIndex(max(self.cb_goal.findData(GOAL_LEAD), 0))

    def _load_plan(self, plan):
        self.ed_name.setText(plan.name or "")
        for cb, val in ((self.cb_promo, plan.promo_type),
                        (self.cb_goal, plan.goal),
                        (self.cb_age, plan.age),
                        (self.cb_gender, plan.gender)):
            i = cb.findData(val)
            if i >= 0:
                cb.setCurrentIndex(i)
        self.sp_budget.setValue(float(plan.budget or 0))
        self.sp_bid.setValue(float(plan.bid or 0))
        self.ed_region.setText(plan.region or "")
        self.ed_hours.setText(plan.hours or "")
        self.ed_target.setText(plan.target_id or "")
        self.ed_uid.setText(plan.douyin_uid or "")
        self.ed_titles.setText("，".join(plan.titles or []))
        self.ed_remark.setText(plan.remark or "")
        self.files.set_paths(list(plan.videos or []))

    def _accept(self):
        name = self.ed_name.text().strip()
        if not name:
            QMessageBox.information(self, "提示", "请填写方案名")
            return
        videos = self.files.paths()
        if not videos:
            QMessageBox.information(self, "提示", "请至少添加 1 条素材视频")
            return
        from video_text_tools.local_push.models import PlanItem
        self._result = PlanItem(
            id=(self._edit.id if self._edit else ""),
            name=name,
            promo_type=self.cb_promo.currentData(),
            goal=self.cb_goal.currentData(),
            budget=float(self.sp_budget.value()),
            bid=float(self.sp_bid.value()),
            region=self.ed_region.text().strip(),
            age=self.cb_age.currentData(),
            gender=self.cb_gender.currentData(),
            hours=self.ed_hours.text().strip(),
            target_id=self.ed_target.text().strip(),
            douyin_uid=self.ed_uid.text().strip(),
            videos=list(videos),
            titles=_split_list(self.ed_titles.text()),
            remark=self.ed_remark.text().strip())
        self.accept()

    def result_plan(self):
        return getattr(self, "_result", None)
