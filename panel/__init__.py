"""Mihomo Web 面板的后端包。

- config     面板设置（文件 + 环境变量）
- httpd      基于标准库的极简 HTTP 框架
- auth       登录与会话
- supervisor 内核进程管理（systemd / OpenRC / 自管）
- mihomo     配置规范化、校验与控制器 API
- profiles   配置仓库与切换逻辑
- routes     HTTP 接口
"""
