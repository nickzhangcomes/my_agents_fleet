# Local release preparation / 本地发布准备

这是一份待审阅的独立源码项目，尚未绑定远程仓库或发布到包索引。项目名为 `my_agents_fleet`，安装包和终端命令名为 `my-agents-fleet`；许可证沿用现有项目的 Apache-2.0。公开发布前需确认维护者署名和版本。

已整理：源码及必要适配层、通用配置、示例、测试、Python 打包配置、命令行入口、中英文 README、许可证、参与开发说明、数据边界与 GitHub Actions 工作流。

发布前由维护者补齐：目标 GitHub 仓库及项目链接；私密漏洞报告渠道；与所支持 CLI 版本对应的真实调用记录。无需把个人模型缓存、登录文件、会话、数据库或本机虚拟环境放进去。

本地验证命令：

```bash
python -m unittest discover -s tests -t .
python scripts/check_repository.py
python -m build
```

打包验证还应在仓库外的干净虚拟环境里安装 wheel（安装包），执行 `my-agents-fleet init/inspect` 和离线演示，确认它没有偷偷依赖源码旁边的私有目录。sdist（源码包）应能独立解压并构建安装包。

GitHub Actions 只配置测试和构建，没有自动发布、上传凭据或调用真实模型的步骤。配置依据官方 [checkout](https://github.com/actions/checkout)、[setup-python](https://github.com/actions/setup-python) 与 [Hatch build](https://hatch.pypa.io/latest/config/build/) 文档；首次托管运行结果仍需在实际仓库中确认。

具体本地验证结果见 [validation.md](validation.md)。
