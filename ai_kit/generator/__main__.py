# -*- coding: utf-8 -*-
"""
python -m ai_kit.generator 的入口 (等价于 python -m ai_kit.generator.core)

用法:
  python -m ai_kit.generator --page "用户管理" \
      --po-module pages.user_management_page --po-class UserManagementPage \
      --fixture user_management_page
"""
from __future__ import annotations

from ai_kit.generator.core import main

if __name__ == "__main__":
    main()
