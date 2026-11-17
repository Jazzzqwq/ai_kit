
# ==================================================
# FM-001
# ==================================================
# -*- coding: utf-8 -*-
"""/front/appointment — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

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

def test_fm_001(driver):
    """FM-001 《汽车销售管理系统-字段格式与边界-请输入您的手机号-错格式》

    form.format.each.请输入您的手机号.wrong · P1
    期望: 提示格式错误且不提交
    前置: 已登录; 拥有该模块的新增/编辑权限
    """
    page.fill(loc("e11"), "@invalid_format")
    page.click(loc("e16"))
    page.assert_field_error(loc("e11"))


# ==================================================
# FM-002
# ==================================================
# -*- coding: utf-8 -*-
"""/front/appointment — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

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

def test_fm_002(driver):
    """FM-002 《汽车销售管理系统-字段格式与边界-请输入备注信息（选填）-错格式》

    form.format.each.请输入备注信息（选填）.wrong · P1
    期望: 提示格式错误且不提交
    前置: 已登录; 拥有该模块的新增/编辑权限
    """
    page.fill(loc("e15"), "@invalid_format")
    page.click(loc("e16"))
    page.assert_field_error(loc("e15"))


# ==================================================
# FM-003
# ==================================================
# -*- coding: utf-8 -*-
"""/front/appointment — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

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

def test_fm_003(driver):
    """FM-003 《汽车销售管理系统-唯一性冲突-请输入您的姓名-提交库里已存在的值》

    form.format.unique.请输入您的姓名.duplicate · P2
    期望: 提示唯一键冲突, 不产生重复记录
    前置: 已登录; 拥有该模块的新增/编辑权限
    """
    page.fill(loc("e10"), "@existing")
    page.click(loc("e16"))
    page.assert_field_error(loc("e10"), "已存在")


# ==================================================
# FM-004
# ==================================================
# -*- coding: utf-8 -*-
"""/front/appointment — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

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

def test_fm_004(driver):
    """FM-004 《汽车销售管理系统-唯一性冲突-请输入您的手机号-提交库里已存在的值》

    form.format.unique.请输入您的手机号.duplicate · P2
    期望: 提示唯一键冲突, 不产生重复记录
    前置: 已登录; 拥有该模块的新增/编辑权限
    """
    page.fill(loc("e11"), "@existing")
    page.click(loc("e16"))
    page.assert_field_error(loc("e11"), "已存在")


# ==================================================
# FM-005
# ==================================================
# -*- coding: utf-8 -*-
"""/front/appointment — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

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

def test_fm_005(driver):
    """FM-005 《汽车销售管理系统-唯一性冲突-请输入备注信息（选填）-提交库里已存在的值》

    form.format.unique.请输入备注信息（选填）.duplicate · P2
    期望: 提示唯一键冲突, 不产生重复记录
    前置: 已登录; 拥有该模块的新增/编辑权限
    """
    page.fill(loc("e15"), "@existing")
    page.click(loc("e16"))
    page.assert_field_error(loc("e15"), "已存在")


# ==================================================
# FM-006
# ==================================================
# -*- coding: utf-8 -*-
"""/front/appointment — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

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

def test_fm_006(driver):
    """FM-006 《汽车销售管理系统-唯一性冲突-字段4-提交库里已存在的值》

    form.format.unique.字段4.duplicate · P2
    期望: 提示唯一键冲突, 不产生重复记录
    前置: 已登录; 拥有该模块的新增/编辑权限
    """
    page.fill(loc("e13"), "@existing")
    page.click(loc("e16"))
    page.assert_field_error(loc("e13"), "已存在")


# ==================================================
# FM-007
# ==================================================
# -*- coding: utf-8 -*-
"""/front/appointment — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

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

def test_fm_007(driver):
    """FM-007 《汽车销售管理系统-唯一性冲突-选择预约时间-提交库里已存在的值》

    form.format.unique.选择预约时间.duplicate · P2
    期望: 提示唯一键冲突, 不产生重复记录
    前置: 已登录; 拥有该模块的新增/编辑权限
    """
    page.fill(loc("e14"), "@existing")
    page.click(loc("e16"))
    page.assert_field_error(loc("e14"), "已存在")


# ==================================================
# FM-008
# ==================================================
# -*- coding: utf-8 -*-
"""/front/appointment — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

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

def test_fm_008(driver):
    """FM-008 《汽车销售管理系统-保存后持久化-提交预约-保存后刷新》

    form.persist.after_save.提交预约.reload · P1
    期望: 刷新页面 (甚至重登) 后数据仍在
    前置: 已登录; 拥有该模块的新增/编辑权限; 使用专用测试数据 (会写库)
    """
    try:
        page.fill(loc("e10"), "QA-FM-008")
        page.click(loc("e16"))
        page.assert_no_error_toast()
        page.reload()
        page.assert_persisted("QA-FM-008")
    finally:          # 清理步骤一定要跑, 否则脏数据留在页面上
        page.reload()


# ==================================================
# FM-009
# ==================================================
# -*- coding: utf-8 -*-
"""/front/appointment — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

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

def test_fm_009(driver):
    """FM-009 《汽车销售管理系统-重复提交-提交预约-双击提交》

    form.submit.double.提交预约.double_click · P1
    期望: 只生成一条记录
    前置: 已登录; 拥有该模块的新增/编辑权限; 使用专用测试数据 (会写库)
    """
    try:
        page.fill(loc("e10"), "QA-FM-009")
        page.double_click(loc("e16"))
        page.assert_no_duplicate_rows(loc("c1"))
    finally:          # 清理步骤一定要跑, 否则脏数据留在页面上
        page.reload()


# ==================================================
# FM-010
# ==================================================
# -*- coding: utf-8 -*-
"""/front/appointment — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

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

def test_fm_010(driver):
    """FM-010 《汽车销售管理系统-提交中被打断-提交预约-提交中返回》

    form.submit.interrupt.提交预约.leave · P2
    期望: 不留在半提交状态: 要么全写要么没写
    前置: 已登录; 拥有该模块的新增/编辑权限; 使用专用测试数据 (会写库)
    """
    try:
        page.fill(loc("e10"), "QA-FM-010")
        page.click(loc("e16"))
        page.back()
        page.reload()
        page.assert_not_persisted("QA-FM-010")
    finally:          # 清理步骤一定要跑, 否则脏数据留在页面上
        page.reload()


# ==================================================
# FM-012
# ==================================================
# -*- coding: utf-8 -*-
"""/front/appointment — standalone 渲染产物 (renderer 生成, eid 对应 locators.json)

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

def test_fm_012(driver):
    """FM-012 《汽车销售管理系统-取消放弃修改-取消预约-改完点取消》

    form.cancel.discard.取消预约.with_changes · P1
    期望: 不落库; 再次进入看不到这次修改
    前置: 已登录; 拥有该模块的新增/编辑权限
    """
    page.fill(loc("e10"), "QA-FM-012")
    page.click(loc("e17"))
    page.reload()
    page.assert_not_persisted("QA-FM-012")

