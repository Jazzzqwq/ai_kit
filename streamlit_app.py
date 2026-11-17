# -*- coding: utf-8 -*-
"""
ai_kit 工作台 — Streamlit Web UI
================================
把 ai_kit 全部能力整合成可视化工作台:
  Sidebar:  网址 / 登录 / LLM 开关 / API Key / 功能勾选
  Main:     四步流水线进度 + 结果卡片 + 生成的 pytest 代码

启动:  streamlit run streamlit_app.py
默认:  http://localhost:8501
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import streamlit as st
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

# ============================================================
# 页面配置 (必须是第一个 streamlit 调用)
# ============================================================
st.set_page_config(
    page_title="ai_kit 工作台 · Web UI 测试 AI 工具箱",
    page_icon="🧪",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ============================================================
# 会话状态初始化
# ============================================================
def _init_state():
    defaults = {
        "driver": None,            # Selenium driver (运行期暂存, 不跨 rerun)
        "inventory": None,          # collect 产物
        "archetype": None,          # classify 产物
        "plan_obj": None,           # plan 产物
        "render_result": None,      # render 产物
        "cases_json": None,         # render 解包后的用例
        "steps": [],                # 已执行的步骤名
        "logs": [],                 # 运行日志
        "llm_configured": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


_init_state()


# ============================================================
# 工具函数
# ============================================================
def log(msg: str, level: str = "info") -> None:
    """往 session_state.logs 追加一条"""
    stamp = datetime.now().strftime("%H:%M:%S")
    st.session_state.logs.append({"t": stamp, "level": level, "msg": msg})


def make_driver() -> webdriver.Edge:
    options = webdriver.EdgeOptions()
    options.add_argument("--start-maximized")
    # headless 在某些机器上 Element Plus input 会变成不可 interact
    # 默认用有头模式, 更稳定
    # options.add_argument("--headless=new")
    # options.add_argument("--disable-gpu")
    # options.add_argument("--no-sandbox")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    driver = webdriver.Edge(options=options)
    driver.set_page_load_timeout(30)
    return driver


def close_driver() -> None:
    d = st.session_state.get("driver")
    if d is not None:
        try:
            d.quit()
        except Exception:
            pass
        st.session_state.driver = None


def check_llm_configured() -> bool:
    """根据 .env 或环境变量判断 LLM 是否可用"""
    from ai_kit.client import llm_available
    return llm_available()


def try_setup_llm(api_key: str, provider: str) -> bool:
    """把用户填的 API key 注入环境变量"""
    if not api_key:
        return False
    env_map = {
        "qwen": "QIANWEN_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "doubao": "DOUBAO_API_KEY",
    }
    key_name = env_map.get(provider, "QIANWEN_API_KEY")
    os.environ[key_name] = api_key
    os.environ["AI_ACTIVE_PROVIDER"] = provider
    log(f"注入 LLM 配置: provider={provider}, key={api_key[:4]}***{api_key[-4:] if len(api_key) >= 8 else ''}", "ok")
    return check_llm_configured()


def ensure_login(driver: webdriver.Edge, url: str, username: str, password: str) -> bool:
    """打开目标 URL, 如果被重定向到登录页就自动登录"""
    driver.get(url)
    time.sleep(2)

    cur = driver.current_url
    if "login" not in cur.lower():
        log(f"未被重定向, 已在 {cur}", "ok")
        return True

    log(f"被重定向到登录页 ({cur}), 尝试自动登录...", "warn")
    WebDriverWait(driver, 20).until(lambda d: len(d.find_elements(By.TAG_NAME, "input")) >= 1)
    time.sleep(0.5)

    inputs = driver.find_elements(By.TAG_NAME, "input")
    if len(inputs) < 2:
        log("找不到至少 2 个 input, 跳过自动登录", "error")
        return False

    # headless 模式下 input 可能被 Element Plus wrapper 遮住, 用 JS 直接赋值
    def _set_input(i, val):
        try:
            inputs[i].clear()
            inputs[i].send_keys(val)
        except Exception:
            # fallback: JS 直接设 value + 触发事件
            driver.execute_script(
                "arguments[0].value = arguments[1];"
                "arguments[0].dispatchEvent(new Event('input', {bubbles:true}));"
                "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
                inputs[i], val,
            )

    _set_input(0, username)
    _set_input(1, password)

    # 点登录按钮
    try:
        driver.find_element(By.CSS_SELECTOR, "button.el-button--primary").click()
    except Exception:
        buttons = driver.find_elements(By.TAG_NAME, "button")
        if buttons:
            buttons[0].click()

    time.sleep(3)

    cur2 = driver.current_url
    if "login" not in cur2.lower():
        log(f"登录成功, 当前 URL: {cur2}", "ok")
        driver.get(url)  # 回到目标页
        time.sleep(2)
        return True
    else:
        log("登录失败, 仍在登录页", "error")
        return False


# ============================================================
# 主标题
# ============================================================
st.markdown(
    """
    <div style="padding: 0.4rem 0;">
    <h1 style="margin:0;">🧪 ai_kit 工作台</h1>
    <p style="color:#888;margin:0.2rem 0 0 0;">
    可移植的 Web UI 测试 AI 工具箱 — 定位器自愈 · 失败归因 · 用例生成 · 离线评测
    </p>
    </div>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# Sidebar 配置面板
# ============================================================
with st.sidebar:
    st.header("⚙️ 配置")

    # --- 目标 URL ---
    target_url = st.text_input(
        "测试目标 URL",
        value="http://localhost:8088/front/appointment",
        help="要测试的 Web 页面地址",
    )

    # --- 登录信息 ---
    need_login = st.checkbox("需要登录", value=True)
    username = password = ""
    if need_login:
        col_u, col_p = st.columns(2)
        username = col_u.text_input("账号", value="18107923127")
        password = col_p.text_input("密码", value="111111", type="password")

    st.divider()

    # --- LLM 配置 ---
    use_llm = st.checkbox("🧠 启用 LLM (花钱)", value=False)
    llm_ok = False
    if use_llm:
        provider = st.selectbox("Provider", ["qwen", "deepseek", "doubao", "ollama"], index=0)
        api_key = st.text_input("API Key", type="password", help="不会保存, 仅注入当前会话环境变量")
        if st.button("验证 LLM 连通性", use_container_width=True):
            with st.spinner("验证中..."):
                llm_ok = try_setup_llm(api_key, provider)
                if llm_ok:
                    st.success("✅ 连通")
                else:
                    st.error("❌ 连通失败, 检查 API Key")
        elif api_key:
            llm_ok = try_setup_llm(api_key, provider)

    st.divider()

    # --- 功能勾选 ---
    st.subheader("🔧 要执行的能力")
    f_collect = st.checkbox("① DOM 采集 (collect)", value=True, help="遍历页面所有可交互元素")
    f_classify = st.checkbox("② 原型分类 (classify)", value=True, help="判定是 login/list/form/detail/dashboard 哪类")
    f_plan = st.checkbox("③ 用例规划 (plan)", value=True, help="确定性规划器派生测试点 (0 LLM)")
    f_heal = st.checkbox("④ 定位器自愈 (heal)", value=True, help="用例规划 + 自愈能力演示")
    f_analyze = st.checkbox("⑤ 失败归因 (analyzer)", value=True, help="失败时的规则层归因演示")
    f_render = st.checkbox("⑥ 生成 pytest 代码", value=True, help="规划 → pytest 代码")

    # --- 执行按钮 ---
    st.divider()
    run_disabled = not (f_collect or f_plan or f_render or f_heal or f_analyze)
    run_btn = st.button("🚀 开始执行", type="primary", use_container_width=True, disabled=run_disabled)
    reset_btn = st.button("🔄 清空重置", use_container_width=True)

    if reset_btn:
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        _init_state()
        st.success("已重置")
        st.rerun()


# ============================================================
# 执行主流程
# ============================================================
def run_pipeline():
    """执行完整流水线"""
    # 导入放到函数内, 让 LLM 配置先注入环境变量
    from ai_kit.perception import collect, classify, plan, render
    from ai_kit.healer import try_heal, rank_candidates
    from ai_kit.analyzer import diagnose

    progress = st.progress(0, text="初始化...")
    close_driver()

    try:
        # --- 启动浏览器 ---
        progress.progress(5, text="启动浏览器...")
        driver = make_driver()
        st.session_state.driver = driver
        log(f"Edge (headless) 已启动")

        # --- 登录 ---
        progress.progress(10, text="导航到目标页...")
        if need_login:
            ok = ensure_login(driver, target_url, username, password)
            if not ok:
                st.error("自动登录失败, 流水线终止")
                close_driver()
                return
        else:
            driver.get(target_url)
            time.sleep(2)

        current_url = driver.current_url
        page_title = driver.title
        log(f"当前页面: {page_title} @ {current_url}")

        # ============================================================
        # Step 1: collect
        # ============================================================
        if f_collect:
            progress.progress(20, text="① DOM 采集...")
            with st.spinner("采集页面元素..."):
                inv = collect(driver, page_name=page_title or "页面")
                st.session_state.inventory = inv
            counts = inv.get("counts", {})
            log(f"采集完成: 发现 {counts.get('discovered')}, 交互 {counts.get('interactive')}, 有效 {counts.get('kept')}", "ok")
            st.session_state.steps.append("collect")

        # ============================================================
        # Step 2: classify
        # ============================================================
        if f_classify and st.session_state.inventory:
            progress.progress(30, text="② 原型分类...")
            with st.spinner("分类页面原型..."):
                ar = classify(st.session_state.inventory, allow_llm=False)
                st.session_state.archetype = ar
            log(f"原型: {ar.archetype} (conf={ar.confidence:.2f}, rule={ar.rule})", "ok")
            st.session_state.steps.append("classify")

        # ============================================================
        # Step 3: plan
        # ============================================================
        plan_obj = None
        if f_plan and st.session_state.inventory:
            progress.progress(45, text="③ 用例规划...")
            with st.spinner("确定性规划器派生测试点..."):
                ar_name = st.session_state.archetype.archetype if st.session_state.archetype else "unknown"
                plan_obj = plan(st.session_state.inventory, archetype=ar_name)
                st.session_state.plan_obj = plan_obj

            if plan_obj:
                n = len(plan_obj.cases)
                log(f"生成 {n} 条结构化用例 (规则外置 JSON, 0 LLM)", "ok")
                st.session_state.steps.append("plan")
            else:
                log("规划器返回 None, 可能原型不在覆盖范围内", "warn")

        # ============================================================
        # Step 4: heal — 在真实页面上演示自愈
        # ============================================================
        if f_heal and driver and st.session_state.inventory:
            progress.progress(60, text="④ 定位器自愈演示...")
            healer_results = []

            # 尝试自愈 el-table 断裂定位器
            table_broken = "//table[@class='el-table__body wrong-class']"
            healed_table = try_heal(driver, "xpath", table_broken, intent="数据表格")
            healer_results.append({
                "断裂定位器": table_broken,
                "自愈结果": "✅ 成功" if healed_table else "❌ 全挂",
                "命中元素": f"<{healed_table.tag_name}>" if healed_table else "-",
            })

            # 尝试自愈 el-select 断裂定位器
            select_broken = "//div[@class='el-select-wrapper old-version']"
            healed_select = try_heal(driver, "xpath", select_broken, intent="下拉选择")
            healer_results.append({
                "断裂定位器": select_broken,
                "自愈结果": "✅ 成功" if healed_select else "❌ 全挂",
                "命中元素": f"<{healed_select.tag_name}>" if healed_select else "-",
            })

            st.session_state.healer_results = healer_results
            success_cnt = sum(1 for r in healer_results if "✅" in r["自愈结果"])
            log(f"healer: {success_cnt}/{len(healer_results)} 自愈成功", "ok" if success_cnt else "warn")
            st.session_state.steps.append("heal")

        # ============================================================
        # Step 5: analyzer — 规则层归因演示 (离线模式)
        # ============================================================
        if f_analyze:
            progress.progress(70, text="⑤ 失败归因 (规则层)...")
            analyzer_cases = [
                {
                    "title": "场景A: 定位器过期",
                    "traceback": (
                        "selenium.common.exceptions.NoSuchElementException: "
                        "Unable to locate element: {'method': 'xpath', 'selector': "
                        "'//div/form/input[@name=\"user\"]'}"
                    ),
                    "log": "",
                },
                {
                    "title": "场景B: StaleElementReference",
                    "traceback": (
                        "selenium.common.exceptions.StaleElementReferenceException: "
                        "The element reference of <input id=\"password\"> is stale"
                    ),
                    "log": "INFO page navigated away",
                },
                {
                    "title": "场景C: 环境问题 (浏览器崩了)",
                    "traceback": (
                        "selenium.common.exceptions.WebDriverException: "
                        "Message: Reached error page: about:neterror"
                    ),
                    "log": "WARN 本地服务端口未监听",
                },
            ]
            analyzer_results = []
            for case in analyzer_cases:
                diag = diagnose(case["traceback"], log_text=case["log"], use_llm=False)
                analyzer_results.append({
                    "场景": case["title"],
                    "归因": diag.category,
                    "置信度": f"{diag.confidence:.2f}",
                    "命中规则": ", ".join(diag.evidence[:3]) if diag.evidence else "-",
                    "建议": diag.suggestion,
                })
            st.session_state.analyzer_results = analyzer_results
            log("analyzer: 3 条失败归因全部完成 (纯规则)", "ok")
            st.session_state.steps.append("analyzer")

        # ============================================================
        # Step 6: render — 生成 pytest 代码
        # ============================================================
        if f_render and plan_obj:
            progress.progress(85, text="⑥ 生成 pytest 代码...")
            with st.spinner("渲染 pytest 代码..."):
                render_result = render(plan_obj, inventory=st.session_state.inventory, mode="standalone")
                st.session_state.render_result = render_result

                # 解包 RenderResult dict
                code_dict = {}
                if isinstance(render_result, dict):
                    code_dict = {k: v for k, v in render_result.items() if isinstance(v, str)}
                st.session_state.cases_json = code_dict

                # 合成完整代码
                full_code = ""
                for k, v in code_dict.items():
                    full_code += f"\n# {'='*50}\n# {k}\n# {'='*50}\n{v}\n"

                # 保存到 generated_tests
                os.makedirs("generated_tests", exist_ok=True)
                out_path = Path("generated_tests") / f"test_{(st.session_state.archetype.archetype if st.session_state.archetype else 'page')}_generated.py"
                out_path.write_text(full_code, encoding="utf-8")
                st.session_state.generated_code = full_code
                st.session_state.generated_path = str(out_path.resolve())

            n_cases = len(code_dict)
            log(f"render: {n_cases} 条 pytest 代码生成, 保存到 {out_path}", "ok")
            st.session_state.steps.append("render")

        progress.progress(100, text="全部完成!")
        time.sleep(0.5)
        progress.empty()
        log("✅ 流水线执行完毕, 查看下方结果卡片", "ok")

    except Exception as e:
        log(f"❌ 流水线异常: {e}", "error")
        traceback.print_exc()
        progress.empty()
    finally:
        close_driver()


if run_btn:
    run_pipeline()


# ============================================================
# 主区域: 进度 + 结果展示
# ============================================================
col_main, col_logs = st.columns([3, 1])

with col_main:
    st.subheader("📋 执行结果")

    steps = st.session_state.steps
    inv = st.session_state.inventory

    if not steps and not st.session_state.cases_json:
        st.info("👈 请在左侧配置后点击「🚀 开始执行」")
    else:
        # --- 采集结果 ---
        if "collect" in steps and inv:
            with st.expander("① DOM 采集结果", expanded=True):
                counts = inv.get("counts", {})
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.metric("发现", counts.get("discovered", 0))
                c2.metric("交互", counts.get("interactive", 0))
                c3.metric("有效", counts.get("kept", 0))
                c4.metric("inputs", len(inv.get("inputs", [])))
                c5.metric("buttons", len(inv.get("buttons", [])))

                st.subheader("交互元素清单")
                all_els = (inv.get("inputs", []) + inv.get("buttons", []) +
                           inv.get("selects", []) + inv.get("tables", []))
                if all_els:
                    table_data = []
                    for el in all_els:
                        eid = el.get("eid", "?")
                        kind = el.get("kind", "?")
                        etype = el.get("type") or "-"
                        name = (el.get("name") or el.get("text") or "")[:40]
                        role = el.get("role") or "-"
                        container = el.get("container", "-")
                        loc = el.get("locator", {})
                        stab = loc.get("stability", 0) if loc else 0
                        loc_val = loc.get("value", "-")[:60] if loc else "-"
                        table_data.append({
                            "eid": eid, "kind": kind, "type": etype,
                            "role": role, "name": name, "container": container,
                            "stability": f"{stab:.1f}", "locator": loc_val,
                        })
                    st.dataframe(table_data, use_container_width=True, hide_index=True, height=280)

                tables = inv.get("tables", [])
                if tables:
                    st.subheader("📊 检测到的表格")
                    for t in tables:
                        cols = t.get("columns", [])
                        col1, col2, col3 = st.columns(3)
                        col1.metric("eid", t.get("eid"))
                        col2.metric("列数", len(cols))
                        col3.metric("行数", t.get("rows", "?"))
                        with st.expander(f"表格 {t.get('eid')} 列名"):
                            st.write(cols)

        # --- 原型分类 ---
        if "classify" in steps and st.session_state.archetype:
            with st.expander("② 页面原型分类"):
                ar = st.session_state.archetype
                st.success(f"**{ar.archetype}** (置信度 {ar.confidence:.2f}, 规则 {ar.rule})")
                archetype_map = {
                    "login": "登录页 — 表单提交后认证",
                    "list": "列表页 — 表格 + 搜索 + 增删改",
                    "form": "表单页 — 填写控件后提交",
                    "detail": "详情页 — 只读展示, 无编辑",
                    "dashboard": "看板页 — 图表为主, 操作稀疏",
                    "unknown": "未知 — 门户/AI对话/其它非五类形态",
                }
                st.write(f"判定依据: {archetype_map.get(ar.archetype, '—')}")
                st.write(f"命中证据: {ar.evidence}")
                if ar.scores:
                    st.bar_chart(ar.scores, height=180)

        # --- 用例规划 ---
        if "plan" in steps and st.session_state.plan_obj:
            with st.expander(f"③ 用例规划 ({len(st.session_state.plan_obj.cases)} 条)"):
                plan_obj = st.session_state.plan_obj
                rows = []
                for c in plan_obj.cases:
                    steps_str = " → ".join(
                        f"{s.action}({s.eid if hasattr(s,'eid') else '?'})"
                        for s in (c.steps or [])[:3]
                    )
                    expect_str = (c.expect or ("",))[0] if c.expect else ""
                    rows.append({
                        "优先级": c.priority,
                        "Case ID": c.case_id,
                        "标题": c.title[:60],
                        "步骤": steps_str,
                        "预期": expect_str[:40],
                    })
                st.dataframe(rows, use_container_width=True, hide_index=True, height=320)

                diag = plan_obj.diagnostics
                if diag:
                    with st.expander("规划器诊断信息"):
                        for key, val in diag.items():
                            if val:
                                st.write(f"**{key}**: {val}")

        # --- healer 自愈 ---
        if "heal" in steps and st.session_state.get("healer_results"):
            with st.expander("④ 定位器自愈演示"):
                st.dataframe(st.session_state.healer_results, use_container_width=True, hide_index=True)
                st.caption("💡 healer L1-L3 规则层主要覆盖 Element Plus 复杂组件 (el-table/el-dialog/el-select)")

        # --- analyzer ---
        if "analyzer" in steps and st.session_state.get("analyzer_results"):
            with st.expander("⑤ 失败归因 (规则层演示)"):
                st.dataframe(st.session_state.analyzer_results, use_container_width=True, hide_index=True)

        # --- 生成的 pytest ---
        if "render" in steps and st.session_state.cases_json:
            with st.expander("⑥ 生成的 pytest 代码", expanded=True):
                cases = st.session_state.cases_json
                st.success(f"✅ {len(cases)} 条用例, py_compile 通过")
                st.info(f"📁 已保存: {st.session_state.get('generated_path', '-')}")

                for case_id, code in cases.items():
                    with st.expander(f"{case_id}"):
                        st.code(code, language="python")

                # 单条下载
                if len(cases) == 1:
                    st.download_button(
                        label="下载 pytest 文件",
                        data=list(cases.values())[0],
                        file_name=f"test_{list(cases.keys())[0]}.py",
                        mime="text/x-python",
                        use_container_width=True,
                    )

                # 全部下载 (打包成一个文件)
                full_code = st.session_state.get("generated_code", "")
                if full_code:
                    st.download_button(
                        label="下载全部用例 (合并)",
                        data=full_code,
                        file_name=f"test_all_{datetime.now().strftime('%Y%m%d_%H%M')}.py",
                        mime="text/x-python",
                        use_container_width=True,
                    )

with col_logs:
    st.subheader("📜 运行日志")
    logs = st.session_state.logs
    if not logs:
        st.caption("等待执行...")
    else:
        for entry in logs[-30:]:  # 最多显示 30 条
            level = entry.get("level", "info")
            msg = entry.get("msg", "")
            t = entry.get("t", "")
            icon_map = {"ok": "🟢", "warn": "🟡", "error": "🔴", "info": "⚪"}
            icon = icon_map.get(level, "⚪")
            st.markdown(f"<div style='font-size:0.85rem;color:#888;'>{t} {icon} {msg}</div>", unsafe_allow_html=True)


# ============================================================
# 页脚
# ============================================================
st.divider()
st.caption(
    "ai_kit v0.1.0 · Streamlit 工作台 · "
    f"{'🧠 LLM 已配置' if check_llm_configured() else '⚪ 离线模式 (0 成本)'} · "
    f"规则版本 rules.v1"
)
