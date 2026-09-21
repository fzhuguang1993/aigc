# video_text_tools/trace_utils.py
"""视频溯源工具（依赖 pymysql；中文首拼可选依赖 pypinyin）

功能：从溯源码池批量取码 -> 按 "溯源码_日期_剪辑首拼_运营首拼.MP4" 规则
重命名视频 -> 溯源记录入库，全程带事务与文件回滚。
"""
import os
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import pymysql

from .config import DB_CFG, TRACE_CONFIG

# 尝试导入 pypinyin
try:
    from pypinyin import lazy_pinyin, Style
    PYPINYIN_AVAILABLE = True
except ImportError:
    PYPINYIN_AVAILABLE = False
    logging.getLogger(__name__).warning("pypinyin 未安装，首拼功能将使用简单替代")

logger = logging.getLogger(__name__)


def get_user_initials(name: str) -> str:
    """
    获取姓名的首拼
    :param name: 中文姓名
    :return: 大写首拼，如 "张三" -> "ZS"
    """
    if not name:
        return "UNKNOWN"

    # 如果 pypinyin 可用，使用 pypinyin
    if PYPINYIN_AVAILABLE:
        try:
            initials = lazy_pinyin(name, style=Style.FIRST_LETTER)
            return ''.join(initials).upper()
        except Exception:
            pass

    # 简单替代方案
    initials = []
    for char in name:
        if '\u4e00' <= char <= '\u9fff':
            initials.append('U')
        elif char.isalpha():
            initials.append(char.upper())
        else:
            initials.append(char)

    return ''.join(initials)[:4] if initials else "UNKNOWN"


def get_trace_owner_map(db_cfg: dict = None) -> dict:
    """
    获取所有溯源码及其归属用户
    :return: {trace_code: user_id}
    """
    conn = None
    cur = None
    try:
        conn = pymysql.connect(**(db_cfg or DB_CFG))
        cur = conn.cursor()
        cur.execute("SELECT trace_code, user_id FROM video_trace")
        results = cur.fetchall()
        return {r[0]: r[1] for r in results}
    except Exception as e:
        logger.error(f"查询溯源码归属失败: {e}")
        return {}
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


class TraceUtils:
    """溯源工具类"""

    def __init__(self, user_info: dict, db_cfg: dict = None):
        """
        初始化
        :param user_info: 当前用户信息，包含 user_id, real_name, role 等
        :param db_cfg: 数据库配置，为空时使用 config.DB_CFG
        """
        self.user_info = user_info
        self.db_cfg = db_cfg or DB_CFG
        self.user_id = user_info.get("user_id")
        self.real_name = user_info.get("real_name", "")
        self.role = user_info.get("role")
        self._is_cancelled = False  # 取消标志

    def cancel(self):
        """取消处理"""
        self._is_cancelled = True
        logger.info("TraceUtils 收到取消信号")

    def get_batch_trace_codes(self, count: int, operator_name: str, conn) -> List[str]:
        """
        批量获取溯源码（使用传入的连接，支持事务）
        :param count: 需要获取的数量
        :param operator_name: 操作人姓名
        :param conn: 数据库连接
        :return: 溯源码列表
        """
        cur = None
        try:
            cur = conn.cursor()

            # 查询多个未使用的溯源码（使用 FOR UPDATE 锁定行）
            sql_select = """
            SELECT trace_code 
            FROM trace_code_pool 
            WHERE is_used = 0 
            ORDER BY create_time ASC 
            LIMIT %s 
            FOR UPDATE
            """
            cur.execute(sql_select, (count,))
            results = cur.fetchall()

            if not results or len(results) < count:
                return []

            trace_codes = [r[0] for r in results]

            # 使用 MySQL 标准日期格式
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 批量更新为已使用
            placeholders = ','.join(['%s'] * len(trace_codes))
            sql_update = f"""
            UPDATE trace_code_pool 
            SET is_used = 1, 
                lock_operator = %s, 
                lock_time = %s,
                update_time = %s
            WHERE trace_code IN ({placeholders})
            """
            cur.execute(sql_update, (
                operator_name,
                now,
                now,
                *trace_codes
            ))

            return trace_codes

        except Exception as e:
            logger.error(f"批量获取溯源码失败: {e}")
            raise
        finally:
            if cur:
                cur.close()

    def save_trace_records(self, records: List[dict], conn) -> bool:
        """批量保存溯源记录到 video_trace 表"""
        cur = None
        try:
            cur = conn.cursor()

            sql = """
            INSERT INTO video_trace (trace_code, user_id, video_path, record_date, create_time)
            VALUES (%s, %s, %s, %s, %s)
            """

            now = datetime.now()
            record_date = now.strftime("%Y-%m-%d")
            create_time = now.strftime("%Y-%m-%d %H:%M:%S")

            for record in records:
                cur.execute(sql, (
                    record["trace_code"],
                    record["user_id"],
                    record["video_path"],
                    record_date,
                    create_time
                ))

            return True

        except Exception as e:
            logger.error(f"批量保存溯源记录失败: {e}")
            raise
        finally:
            if cur:
                cur.close()

    def generate_new_filename(
            self,
            trace_code: str,
            video_path: str,
            editor_name: str,
            operator_name: str
    ) -> str:
        """
        生成新的文件名
        格式: 溯源码_日期_剪辑姓名首拼_运营首拼.MP4
        """
        date_str = datetime.now().strftime(TRACE_CONFIG.get("date_format", "%Y%m%d"))
        editor_initials = get_user_initials(editor_name)
        operator_initials = get_user_initials(operator_name)
        ext = Path(video_path).suffix or ".MP4"
        ext = ext.upper()

        new_name = f"{trace_code}_{date_str}_{editor_initials}_{operator_initials}{ext}"
        return new_name

    def rename_video(self, old_path: str, new_name: str) -> Tuple[bool, str]:
        """重命名视频文件（重名自动加序号后缀）"""
        try:
            dir_path = os.path.dirname(old_path)
            new_path = os.path.join(dir_path, new_name)

            if os.path.exists(new_path):
                base, ext = os.path.splitext(new_name)
                counter = 1
                while os.path.exists(os.path.join(dir_path, f"{base}_{counter}{ext}")):
                    counter += 1
                new_path = os.path.join(dir_path, f"{base}_{counter}{ext}")

            os.rename(old_path, new_path)
            return True, new_path
        except Exception as e:
            return False, str(e)

    def process_videos(
            self,
            video_paths: List[str],
            operator_name: str = None,
            operator_id: int = None,
            progress_callback=None,
            log_callback=None
    ) -> Dict[str, dict]:
        """
        批量处理视频溯源（带事务管理，失败时回滚数据库与文件重命名）
        :param video_paths: 视频路径列表
        :param operator_name: 运营姓名
        :param operator_id: 运营ID
        :param progress_callback: callable(percent)
        :param log_callback: callable(message)
        :return: {video_path: {"success", "new_path", "error", "trace_code", "old_path"}}
        """
        results = {}
        conn = None
        processed_files = []  # 记录已重命名的文件，用于回滚
        trace_records = []    # 记录要保存的溯源记录

        if not operator_name:
            operator_name = self.real_name

        if operator_id is None:
            operator_id = self.user_id

        editor_name = self.real_name
        total = len(video_paths)

        try:
            # 1. 建立数据库连接并开启事务
            conn = pymysql.connect(**self.db_cfg)
            conn.begin()

            if log_callback:
                log_callback(f"📦 开始事务处理，共 {total} 个视频")
                log_callback(f"👤 剪辑: {editor_name}")
                log_callback(f"👤 运营: {operator_name}")

            # 检查取消标志
            if self._is_cancelled:
                raise Exception("用户取消了操作")

            # 2. 批量获取溯源码
            if log_callback:
                log_callback(f"🔍 正在从溯源码池获取 {total} 个溯源码...")

            trace_codes = self.get_batch_trace_codes(total, operator_name, conn)

            if not trace_codes:
                raise Exception("溯源码池中没有可用的溯源码！")

            if len(trace_codes) < total:
                raise Exception(f"溯源码不足！需要 {total} 个，实际获取 {len(trace_codes)} 个")

            if log_callback:
                log_callback(f"✅ 成功获取 {len(trace_codes)} 个溯源码")

            # 3. 逐个处理视频
            for idx, video_path in enumerate(video_paths):
                # 检查取消标志
                if self._is_cancelled:
                    raise Exception("用户取消了操作")

                result = {
                    "success": False,
                    "new_path": None,
                    "error": None,
                    "trace_code": None,
                    "old_path": video_path
                }

                try:
                    # 更新进度
                    if progress_callback:
                        progress_callback(int((idx + 1) / total * 100))

                    trace_code = trace_codes[idx]
                    result["trace_code"] = trace_code

                    if log_callback:
                        log_callback(f"📝 [{idx + 1}/{total}] 处理: {Path(video_path).name}")
                        log_callback(f"   └─ 溯源码: {trace_code}")

                    # 生成新文件名
                    new_name = self.generate_new_filename(
                        trace_code, video_path, editor_name, operator_name
                    )

                    if log_callback:
                        log_callback(f"   └─ 新文件名: {new_name}")

                    # 重命名
                    success, new_path = self.rename_video(video_path, new_name)

                    if success:
                        result["success"] = True
                        result["new_path"] = new_path
                        processed_files.append((video_path, new_path))

                        # 记录要保存的溯源记录
                        trace_records.append({
                            "trace_code": trace_code,
                            "video_path": new_path,
                            "user_id": self.user_id
                        })

                        if log_callback:
                            log_callback("   └─ ✅ 重命名成功")
                    else:
                        result["error"] = new_path
                        raise Exception(f"重命名失败: {new_path}")

                except Exception as e:
                    result["error"] = str(e)
                    if log_callback:
                        log_callback(f"   └─ ❌ 处理失败: {str(e)}")
                    # 单个文件失败，整个事务回滚
                    raise Exception(f"处理 {Path(video_path).name} 失败: {str(e)}")

                results[video_path] = result

            # 检查取消标志
            if self._is_cancelled:
                raise Exception("用户取消了操作")

            # 4. 保存溯源记录到数据库
            if trace_records:
                if log_callback:
                    log_callback("💾 保存溯源记录到数据库...")
                self.save_trace_records(trace_records, conn)
                if log_callback:
                    log_callback(f"✅ {len(trace_records)} 条溯源记录保存成功")

            # 5. 提交事务
            conn.commit()
            if log_callback:
                log_callback("✅ 事务提交成功")

            # 6. 返回结果
            success_count = sum(1 for r in results.values() if r['success'])
            if log_callback:
                log_callback(f"🎉 处理完成！成功: {success_count} 个")

            return results

        except Exception as e:
            # 发生错误，回滚事务
            if conn:
                conn.rollback()
                if log_callback:
                    log_callback(f"❌ 发生错误，事务已回滚: {str(e)}")

            # 回滚文件重命名（将已重命名的文件恢复原名称）
            if log_callback:
                log_callback("🔄 正在回滚文件重命名...")

            for old_path, new_path in processed_files:
                try:
                    if os.path.exists(new_path):
                        os.rename(new_path, old_path)
                        if log_callback:
                            log_callback(f"   └─ 已恢复: {Path(old_path).name}")
                except Exception as rollback_err:
                    if log_callback:
                        log_callback(f"   └─ ⚠️ 回滚失败: {Path(old_path).name} - {rollback_err}")

            if log_callback:
                log_callback("🔄 回滚完成")

            # 更新所有结果为失败
            for video_path in video_paths:
                if video_path in results:
                    results[video_path]["success"] = False
                    if not results[video_path]["error"]:
                        results[video_path]["error"] = str(e)
                else:
                    results[video_path] = {
                        "success": False,
                        "new_path": None,
                        "error": str(e),
                        "trace_code": None,
                        "old_path": video_path
                    }

            return results

        finally:
            if conn:
                conn.close()


def bind_videos(
        parent_trace_code: str,
        child_trace_codes: List[str],
        user_id: int = None,
        db_cfg: dict = None,
        log_callback=None,
) -> dict:
    """批量绑定子视频到母版（从 BindWorker 提取，无 UI 依赖）

    :return: {"success": n, "failed": n}
    """
    return _bind_operation("bind", parent_trace_code, child_trace_codes,
                           user_id, db_cfg, log_callback)


def unbind_videos(
        parent_trace_code: str,
        child_trace_codes: List[str],
        db_cfg: dict = None,
        log_callback=None,
) -> dict:
    """批量解绑子视频与母版的绑定关系

    :return: {"success": n, "failed": n}
    """
    return _bind_operation("unbind", parent_trace_code, child_trace_codes,
                           None, db_cfg, log_callback)


def _bind_operation(action, parent_trace_code, child_trace_codes,
                    user_id, db_cfg, log_callback) -> dict:
    log = log_callback or (lambda m: None)
    conn = None
    cur = None
    success_count = 0
    fail_count = 0

    try:
        conn = pymysql.connect(**(db_cfg or DB_CFG))
        cur = conn.cursor()

        if action == "bind":
            cur.execute("SELECT id FROM video_trace WHERE trace_code = %s", (parent_trace_code,))
            if not cur.fetchone():
                raise Exception(f"母版溯源码 {parent_trace_code} 不存在")

            for child_code in child_trace_codes:
                try:
                    cur.execute("SELECT id FROM video_trace WHERE trace_code = %s", (child_code,))
                    if not cur.fetchone():
                        log(f"⚠️ 子视频 {child_code} 不存在，跳过")
                        fail_count += 1
                        continue

                    cur.execute("SELECT id FROM video_bind WHERE trace_code = %s", (child_code,))
                    if cur.fetchone():
                        log(f"⚠️ {child_code} 已被绑定，跳过")
                        fail_count += 1
                        continue

                    cur.execute("SELECT id FROM video_bind WHERE trace_code = %s", (parent_trace_code,))
                    if cur.fetchone():
                        log(f"⚠️ {parent_trace_code} 本身已被绑定，不能作为母版")
                        fail_count += 1
                        continue

                    cur.execute("""
                        INSERT INTO video_bind (trace_code, bind_trace_code, user_id, bind_time)
                        VALUES (%s, %s, %s, NOW())
                    """, (child_code, parent_trace_code, user_id))

                    log(f"✅ {child_code} → {parent_trace_code}")
                    success_count += 1

                except Exception as e:
                    log(f"❌ {child_code} 绑定失败: {str(e)}")
                    fail_count += 1

            conn.commit()

        elif action == "unbind":
            for child_code in child_trace_codes:
                try:
                    cur.execute("""
                        DELETE FROM video_bind 
                        WHERE trace_code = %s AND bind_trace_code = %s
                    """, (child_code, parent_trace_code))

                    if cur.rowcount > 0:
                        log(f"✅ 已解绑: {child_code} ← {parent_trace_code}")
                        success_count += 1
                    else:
                        log(f"⚠️ 未找到绑定关系: {child_code} → {parent_trace_code}")
                        fail_count += 1
                except Exception as e:
                    log(f"❌ 解绑失败 {child_code}: {str(e)}")
                    fail_count += 1

            conn.commit()

    except Exception as e:
        if conn:
            conn.rollback()
        log(f"❌ 操作失败: {str(e)}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    return {"success": success_count, "failed": fail_count}

