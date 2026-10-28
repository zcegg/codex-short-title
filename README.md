# Codex 短标题

基于 [oil-oil/oil-codex-title](https://github.com/oil-oil/oil-codex-title) 的 MIT 许可定制版。保留原作者版权与后台命名机制，增加可配置的短标题模式。基于上游提交 `ae1c9e2` 的本地安装版本。

## 效果

`🧩 购物车｜修复金额计算` → `购物车金额修复`

- short：对象＋动作或核心目标；无 emoji、类别或分隔符，中文优先 6～16 字，上限 32 字符。
- classic：原版 emoji 对象｜目标；为兼容已有用户，未配置时仍使用 classic。
- 保留锁定、手动标题保护、过期结果丢弃与写入读回验证。
- 可选自动归组：只处理启用后新建、未归组的聊天，不新建分组，不批量整理历史聊天。
- 不自动归档。恢复旧聊天后，仅未锁定且未手工保护的聊天参与后续命名。

## 安装与开启

需要 Python 3.10+、已登录的 Codex CLI，以及支持插件 Stop Hook 的本地 Codex。

此仓库保留插件 ID `oil-codex-title`，便于延续原锁定记录；不要与原版同时启用。

```sh
codex plugin marketplace add https://github.com/zcegg/codex-short-title.git
codex plugin add oil-codex-title@codex-short-title
```

安装后通过插件技能入口设置（把 `<插件目录>` 换成实际安装目录）：

```sh
python3 <插件目录>/skills/oil-codex-title/scripts/run.py configure --title-style short
python3 <插件目录>/skills/oil-codex-title/scripts/run.py status
```

配置应显示 `title_style: short`。在 Codex `/hooks` 中检查并信任命令；更新后若显示待信任，需重新确认。不能通过编辑信任数据库绕过此步骤。是否需要重启由宿主插件刷新状态决定。

使用当前账号的模型额度；独立命名模型只接收最近对话摘录和首轮目标，没有工具权限。配置、锁定记录、日志仍保存在本机 `~/.codex/oil-codex-title`，不会提交到本仓库。

## 自动归组（默认关闭）

```sh
python3 <插件目录>/skills/oil-codex-title/scripts/run.py configure --auto-group on
python3 <插件目录>/skills/oil-codex-title/scripts/run.py configure --auto-group off
```

开启时记录时间边界，重复开启不重置边界。关闭后再开启从新的时间开始。仅本地持久化的用户聊天参与；临时模型与子代理会话跳过。

每轮结束时先命名，再检查归组资格；仅启用后新建、没有项目或分组、未归档、未锁定的聊天参与。优先按工作目录匹配已有项目，嵌套项目取最具体的目录；同路径对应多个项目则保留原位。无项目匹配时，独立模型根据对话主线选择已有分组；不确定则留在原位，不将“临时处理”当作兜底。

每个聊天最多做一次归组决策；后续手动移出不会被自动搬回。判断期间发现手动移动、新轮次、归档、锁定或关闭开关时取消写入。通过官方 `thread/metadata/update` 或 `thread/section/move` 写入并读回验证，不修改数据库或桌面缓存。

模型归组使用现有账号模型额度；按项目目录匹配不额外调用模型。只处理当前 Stop 事件，不遍历改写历史聊天。命名失败时本轮不执行归组，后续正常轮次再检查。

限制：官方接口没有原子条件写入，最后一次检查与写入之间极短窗口内的手动移动仍可能竞争；写入结果不确定时保留尝试记录且不自动重试。服务端归属与桌面展示分别验证；旧版 Codex 缺少接口时本次保留原位并记录错误。

## 固定标题与回退

```sh
python3 <插件目录>/skills/oil-codex-title/scripts/run.py lock <话题ID>
python3 <插件目录>/skills/oil-codex-title/scripts/run.py configure --title-style classic
python3 <插件目录>/skills/oil-codex-title/scripts/run.py pause
```

切换风格不会解除锁定或清空记录。覆盖安装前备份原插件源目录；需要回到原版代码时恢复原目录并通过 `codex plugin add` 重装。

## 验证

```sh
python3 -m unittest discover -s tests
```

自动化测试不调用模型。`tests/test_short_title.py` 覆盖格式、配置、提示词选择、缓存失效、锁定和异步切换风格。
真实模型验证与真实宿主 Hook 触发是两种证据，不能互相替代。详情见 [回归规范](docs/命名与回归规范.md)。原版功能与历史说明见 [上游 README](README.upstream.md)。
