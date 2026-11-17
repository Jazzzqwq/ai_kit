
# ==================================================
# DT-001
# ==================================================
# -*- coding: utf-8 -*-
"""/auth/login — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

重新渲染会覆盖本文件。要改行为, 改计划或 ai_kit/runtime 的原语。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_kit.runtime import loc, page, use_locators

use_locators(Path(__file__).with_name("locators.json"))


@pytest.fixture(autouse=True)
def _ai_kit_page(driver):
    page.bind(driver)
    try:
        yield
    finally:
        page.unbind()

def test_dt_001(driver):
    """DT-001 《登录-敏感字段脱敏-整页-查看敏感字段》

    detail.masking.sensitive.整页.masked · P1
    期望: 手机号/证件号/密码等按需脱敏; 前端接口里不返回明文
    前置: 已登录; 列表中存在至少 1 条记录
    """
    page.assert_masked()


# ==================================================
# DT-002
# ==================================================
# -*- coding: utf-8 -*-
"""/auth/login — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

重新渲染会覆盖本文件。要改行为, 改计划或 ai_kit/runtime 的原语。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_kit.runtime import loc, page, use_locators

use_locators(Path(__file__).with_name("locators.json"))


@pytest.fixture(autouse=True)
def _ai_kit_page(driver):
    page.bind(driver)
    try:
        yield
    finally:
        page.unbind()

def test_dt_002(driver):
    """DT-002 《登录-操作按钮权限-注册-有权限》

    detail.actions.permission.注册.normal · P1
    期望: 编辑/删除入口可见且可用
    前置: 已登录; 列表中存在至少 1 条记录
    """
    page.assert_visible(loc("e1"))
    page.click(loc("e1"))
    page.assert_feedback()


# ==================================================
# DT-003
# ==================================================
# -*- coding: utf-8 -*-
"""/auth/login — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

重新渲染会覆盖本文件。要改行为, 改计划或 ai_kit/runtime 的原语。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_kit.runtime import loc, page, use_locators

use_locators(Path(__file__).with_name("locators.json"))


@pytest.fixture(autouse=True)
def _ai_kit_page(driver):
    page.bind(driver)
    try:
        yield
    finally:
        page.unbind()

def test_dt_003(driver):
    """DT-003 《登录-操作按钮权限-注册-无权限账号》

    detail.actions.permission.注册.readonly · P2
    期望: 入口不显示或置灰; 直接改 URL 也应被拦
    前置: 已登录; 列表中存在至少 1 条记录; 使用只读账号登录
    """
    page.assert_inactive(loc("e1"))


# ==================================================
# DT-004
# ==================================================
# -*- coding: utf-8 -*-
"""/auth/login — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

重新渲染会覆盖本文件。要改行为, 改计划或 ai_kit/runtime 的原语。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_kit.runtime import loc, page, use_locators

use_locators(Path(__file__).with_name("locators.json"))


@pytest.fixture(autouse=True)
def _ai_kit_page(driver):
    page.bind(driver)
    try:
        yield
    finally:
        page.unbind()

def test_dt_004(driver):
    """DT-004 《登录-操作按钮权限-旧版用户迁移-有权限》

    detail.actions.permission.旧版用户迁移.normal · P1
    期望: 编辑/删除入口可见且可用
    前置: 已登录; 列表中存在至少 1 条记录
    """
    page.assert_visible(loc("e2"))
    page.click(loc("e2"))
    page.assert_feedback()


# ==================================================
# DT-005
# ==================================================
# -*- coding: utf-8 -*-
"""/auth/login — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

重新渲染会覆盖本文件。要改行为, 改计划或 ai_kit/runtime 的原语。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_kit.runtime import loc, page, use_locators

use_locators(Path(__file__).with_name("locators.json"))


@pytest.fixture(autouse=True)
def _ai_kit_page(driver):
    page.bind(driver)
    try:
        yield
    finally:
        page.unbind()

def test_dt_005(driver):
    """DT-005 《登录-操作按钮权限-旧版用户迁移-无权限账号》

    detail.actions.permission.旧版用户迁移.readonly · P2
    期望: 入口不显示或置灰; 直接改 URL 也应被拦
    前置: 已登录; 列表中存在至少 1 条记录; 使用只读账号登录
    """
    page.assert_inactive(loc("e2"))


# ==================================================
# DT-006
# ==================================================
# -*- coding: utf-8 -*-
"""/auth/login — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

重新渲染会覆盖本文件。要改行为, 改计划或 ai_kit/runtime 的原语。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_kit.runtime import loc, page, use_locators

use_locators(Path(__file__).with_name("locators.json"))


@pytest.fixture(autouse=True)
def _ai_kit_page(driver):
    page.bind(driver)
    try:
        yield
    finally:
        page.unbind()

def test_dt_006(driver):
    """DT-006 《登录-操作按钮权限-进入系统-有权限》

    detail.actions.permission.进入系统.normal · P1
    期望: 编辑/删除入口可见且可用
    前置: 已登录; 列表中存在至少 1 条记录
    """
    page.assert_visible(loc("e3"))
    page.click(loc("e3"))
    page.assert_feedback()


# ==================================================
# DT-007
# ==================================================
# -*- coding: utf-8 -*-
"""/auth/login — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

重新渲染会覆盖本文件。要改行为, 改计划或 ai_kit/runtime 的原语。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_kit.runtime import loc, page, use_locators

use_locators(Path(__file__).with_name("locators.json"))


@pytest.fixture(autouse=True)
def _ai_kit_page(driver):
    page.bind(driver)
    try:
        yield
    finally:
        page.unbind()

def test_dt_007(driver):
    """DT-007 《登录-操作按钮权限-进入系统-无权限账号》

    detail.actions.permission.进入系统.readonly · P2
    期望: 入口不显示或置灰; 直接改 URL 也应被拦
    前置: 已登录; 列表中存在至少 1 条记录; 使用只读账号登录
    """
    page.assert_inactive(loc("e3"))


# ==================================================
# DT-008
# ==================================================
# -*- coding: utf-8 -*-
"""/auth/login — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

重新渲染会覆盖本文件。要改行为, 改计划或 ai_kit/runtime 的原语。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_kit.runtime import loc, page, use_locators

use_locators(Path(__file__).with_name("locators.json"))


@pytest.fixture(autouse=True)
def _ai_kit_page(driver):
    page.bind(driver)
    try:
        yield
    finally:
        page.unbind()

def test_dt_008(driver):
    """DT-008 《登录-操作按钮权限-登出-有权限》

    detail.actions.permission.登出.normal · P1
    期望: 编辑/删除入口可见且可用
    前置: 已登录; 列表中存在至少 1 条记录
    """
    page.assert_visible(loc("e4"))
    page.click(loc("e4"))
    page.assert_feedback()


# ==================================================
# DT-009
# ==================================================
# -*- coding: utf-8 -*-
"""/auth/login — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

重新渲染会覆盖本文件。要改行为, 改计划或 ai_kit/runtime 的原语。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_kit.runtime import loc, page, use_locators

use_locators(Path(__file__).with_name("locators.json"))


@pytest.fixture(autouse=True)
def _ai_kit_page(driver):
    page.bind(driver)
    try:
        yield
    finally:
        page.unbind()

def test_dt_009(driver):
    """DT-009 《登录-操作按钮权限-登出-无权限账号》

    detail.actions.permission.登出.readonly · P2
    期望: 入口不显示或置灰; 直接改 URL 也应被拦
    前置: 已登录; 列表中存在至少 1 条记录; 使用只读账号登录
    """
    page.assert_inactive(loc("e4"))


# ==================================================
# DT-010
# ==================================================
# -*- coding: utf-8 -*-
"""/auth/login — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

重新渲染会覆盖本文件。要改行为, 改计划或 ai_kit/runtime 的原语。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_kit.runtime import loc, page, use_locators

use_locators(Path(__file__).with_name("locators.json"))


@pytest.fixture(autouse=True)
def _ai_kit_page(driver):
    page.bind(driver)
    try:
        yield
    finally:
        page.unbind()

def test_dt_010(driver):
    """DT-010 《登录-操作按钮权限-浙ICP备17025079号-1-有权限》

    detail.actions.permission.浙ICP备17025079号-1.normal · P1
    期望: 编辑/删除入口可见且可用
    前置: 已登录; 列表中存在至少 1 条记录
    """
    page.assert_visible(loc("e5"))
    page.click(loc("e5"))
    page.assert_feedback()


# ==================================================
# DT-011
# ==================================================
# -*- coding: utf-8 -*-
"""/auth/login — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

重新渲染会覆盖本文件。要改行为, 改计划或 ai_kit/runtime 的原语。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_kit.runtime import loc, page, use_locators

use_locators(Path(__file__).with_name("locators.json"))


@pytest.fixture(autouse=True)
def _ai_kit_page(driver):
    page.bind(driver)
    try:
        yield
    finally:
        page.unbind()

def test_dt_011(driver):
    """DT-011 《登录-操作按钮权限-浙ICP备17025079号-1-无权限账号》

    detail.actions.permission.浙ICP备17025079号-1.readonly · P2
    期望: 入口不显示或置灰; 直接改 URL 也应被拦
    前置: 已登录; 列表中存在至少 1 条记录; 使用只读账号登录
    """
    page.assert_inactive(loc("e5"))


# ==================================================
# DT-012
# ==================================================
# -*- coding: utf-8 -*-
"""/auth/login — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

重新渲染会覆盖本文件。要改行为, 改计划或 ai_kit/runtime 的原语。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_kit.runtime import loc, page, use_locators

use_locators(Path(__file__).with_name("locators.json"))


@pytest.fixture(autouse=True)
def _ai_kit_page(driver):
    page.bind(driver)
    try:
        yield
    finally:
        page.unbind()

def test_dt_012(driver):
    """DT-012 《登录-操作按钮权限-浙公网安备 33010602010806号-有权限》

    detail.actions.permission.浙公网安备_33010602010806号.normal · P1
    期望: 编辑/删除入口可见且可用
    前置: 已登录; 列表中存在至少 1 条记录
    """
    page.assert_visible(loc("e6"))
    page.click(loc("e6"))
    page.assert_feedback()


# ==================================================
# DT-013
# ==================================================
# -*- coding: utf-8 -*-
"""/auth/login — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

重新渲染会覆盖本文件。要改行为, 改计划或 ai_kit/runtime 的原语。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_kit.runtime import loc, page, use_locators

use_locators(Path(__file__).with_name("locators.json"))


@pytest.fixture(autouse=True)
def _ai_kit_page(driver):
    page.bind(driver)
    try:
        yield
    finally:
        page.unbind()

def test_dt_013(driver):
    """DT-013 《登录-操作按钮权限-浙公网安备 33010602010806号-无权限账号》

    detail.actions.permission.浙公网安备_33010602010806号.readonly · P2
    期望: 入口不显示或置灰; 直接改 URL 也应被拦
    前置: 已登录; 列表中存在至少 1 条记录; 使用只读账号登录
    """
    page.assert_inactive(loc("e6"))


# ==================================================
# DT-014
# ==================================================
# -*- coding: utf-8 -*-
"""/auth/login — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

重新渲染会覆盖本文件。要改行为, 改计划或 ai_kit/runtime 的原语。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_kit.runtime import loc, page, use_locators

use_locators(Path(__file__).with_name("locators.json"))


@pytest.fixture(autouse=True)
def _ai_kit_page(driver):
    page.bind(driver)
    try:
        yield
    finally:
        page.unbind()

def test_dt_014(driver):
    """DT-014 《登录-操作按钮权限-主页-有权限》

    detail.actions.permission.主页.normal · P1
    期望: 编辑/删除入口可见且可用
    前置: 已登录; 列表中存在至少 1 条记录
    """
    page.assert_visible(loc("e7"))
    page.click(loc("e7"))
    page.assert_feedback()


# ==================================================
# DT-015
# ==================================================
# -*- coding: utf-8 -*-
"""/auth/login — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

重新渲染会覆盖本文件。要改行为, 改计划或 ai_kit/runtime 的原语。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_kit.runtime import loc, page, use_locators

use_locators(Path(__file__).with_name("locators.json"))


@pytest.fixture(autouse=True)
def _ai_kit_page(driver):
    page.bind(driver)
    try:
        yield
    finally:
        page.unbind()

def test_dt_015(driver):
    """DT-015 《登录-操作按钮权限-主页-无权限账号》

    detail.actions.permission.主页.readonly · P2
    期望: 入口不显示或置灰; 直接改 URL 也应被拦
    前置: 已登录; 列表中存在至少 1 条记录; 使用只读账号登录
    """
    page.assert_inactive(loc("e7"))


# ==================================================
# DT-016
# ==================================================
# -*- coding: utf-8 -*-
"""/auth/login — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

重新渲染会覆盖本文件。要改行为, 改计划或 ai_kit/runtime 的原语。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_kit.runtime import loc, page, use_locators

use_locators(Path(__file__).with_name("locators.json"))


@pytest.fixture(autouse=True)
def _ai_kit_page(driver):
    page.bind(driver)
    try:
        yield
    finally:
        page.unbind()

def test_dt_016(driver):
    """DT-016 《登录-操作按钮权限-关于我们-有权限》

    detail.actions.permission.关于我们.normal · P1
    期望: 编辑/删除入口可见且可用
    前置: 已登录; 列表中存在至少 1 条记录
    """
    page.assert_visible(loc("e8"))
    page.click(loc("e8"))
    page.assert_feedback()


# ==================================================
# DT-017
# ==================================================
# -*- coding: utf-8 -*-
"""/auth/login — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

重新渲染会覆盖本文件。要改行为, 改计划或 ai_kit/runtime 的原语。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_kit.runtime import loc, page, use_locators

use_locators(Path(__file__).with_name("locators.json"))


@pytest.fixture(autouse=True)
def _ai_kit_page(driver):
    page.bind(driver)
    try:
        yield
    finally:
        page.unbind()

def test_dt_017(driver):
    """DT-017 《登录-操作按钮权限-关于我们-无权限账号》

    detail.actions.permission.关于我们.readonly · P2
    期望: 入口不显示或置灰; 直接改 URL 也应被拦
    前置: 已登录; 列表中存在至少 1 条记录; 使用只读账号登录
    """
    page.assert_inactive(loc("e8"))


# ==================================================
# DT-018
# ==================================================
# -*- coding: utf-8 -*-
"""/auth/login — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

重新渲染会覆盖本文件。要改行为, 改计划或 ai_kit/runtime 的原语。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_kit.runtime import loc, page, use_locators

use_locators(Path(__file__).with_name("locators.json"))


@pytest.fixture(autouse=True)
def _ai_kit_page(driver):
    page.bind(driver)
    try:
        yield
    finally:
        page.unbind()

def test_dt_018(driver):
    """DT-018 《登录-返回列表-整页-从列表点进来再返回》

    detail.navigation.back.整页.keep_filter · P1
    期望: 回到列表时筛选/页码保持 (或被明确重置并提示)
    前置: 已登录; 列表中存在至少 1 条记录
    """
    page.back()
    page.assert_no_error_toast()

