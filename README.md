# niri-scratchpad

通过 Niri IPC 管理任意现有窗口的暂存区。只使用 Python 标准库，不需要修改 Niri，也不需要后台守护进程。

- 收起窗口时移入命名工作区 `scratch`，应用继续运行。
- 呼出时临时浮动，初始区域为 **954×1028 逻辑像素、居中**。这是提取前的实际窗口尺寸，避免继承最大化应用的整屏大小。
- 循环切换时，下一窗口沿用同一悬浮区域。拖动或缩放后，后续切换沿用新区域。
- 再次收起后恢复该窗口原来的平铺／浮动模式和浮动几何；背景窗口布局不变。
- 支持跨显示器呼出。误入暂存工作区、原工作区因空置消失时可返回普通工作区。

## 快捷键

| 按键 | 操作 |
| --- | --- |
| `Mod+Shift+反引号` | 收起当前窗口 |
| `Mod+反引号` | 呼出／收起；失焦时先重新聚焦 |
| `Mod+Ctrl+反引号` | 在同一悬浮区域循环切换下一个窗口 |

`Mod` 使用你现有的 Niri 设置；例如 `mod-key "Super"` 时就是 Win 键。反引号是美式键盘 Esc 下方的键。

## 自动循环其他工作区窗口（本地扩展）

`toggle` 和 `next` 支持 `--all-workspaces`：

```sh
python3 "${XDG_CONFIG_HOME:-$HOME/.config}/niri/scratchpad.py" toggle --all-workspaces
python3 "${XDG_CONFIG_HOME:-$HOME/.config}/niri/scratchpad.py" next --all-workspaces
```

在 `scratchpad.kdl` 对应的两条命令末尾加上该参数即可启用。仓库默认配置仍保留手动模式。

- 自动发现其他工作区（包括其他显示器）的窗口，无需逐个手动收起；当前工作区的普通窗口不加入候选列表。
- 保留手动暂存的成员，新开或关闭的窗口会在下次按键时更新。
- 自动借用的窗口在收起或切换时返回原工作区，恢复原来的平铺／浮动模式和已记录的尺寸；手动暂存成员仍返回 `scratch`。
- 在呼出窗口上主动执行 `stash`，会把它改为手动暂存成员；`release` 则保留当前位置并解除恢复记录。
- 无名的来源工作区借用期间会临时命名为 `scratchpad-return-…`，以免移走最后一个窗口后被 Niri 删除；窗口归还或关闭后，下次调用脚本会清理临时名称。用户另行设置的名字不会被覆盖。
- 平铺窗口归还时重新加入布局，不保证恢复其原来的列顺序、分组或自动高度分配策略；浮动位置和实际尺寸仍受应用与显示器约束。
- 若来源工作区因显示器移除等原因确实不存在，窗口退回可访问的 `scratch`，并在标准错误中说明；运行时状态丢失后无法恢复借用前的工作区信息。

## 普通安装

需要运行中的 Niri 和 Python 3。以下命令只安装两个文件，不改动 Niri 主配置或其他应用配置。已有同名文件时请先核对内容。

```sh
git clone https://github.com/hecongnan/niri-scratchpad.git
cd niri-scratchpad
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/niri"
install -Dm644 scratchpad.py "$config_dir/scratchpad.py"
install -Dm644 scratchpad.kdl "$config_dir/scratchpad.kdl"
```

在现有 Niri 主配置中添加一次：

```kdl
include "scratchpad.kdl"
```

验证并重载：

```sh
niri validate
niri msg action load-config-file
```

不需要应用任何完整的 NixOS 配置。

## Home Manager / NixOS

仓库提供一个没有外部输入的 Flake 模块，既可直接导入文件，也可作为 Flake 输入。

```nix
# flake.nix 的 inputs
niri-scratchpad.url = "github:hecongnan/niri-scratchpad";
```

在你的 Home Manager 用户模块中导入：

```nix
imports = [ inputs.niri-scratchpad.homeManagerModules.default ];
```

该模块仅部署 `niri/scratchpad.py` 和 `niri/scratchpad.kdl`，使用 Nix 提供的 Python 绝对路径。你仍需在自己现有的 Niri 配置里添加上面的 `include`，模块不会接管它。

集成式 Home Manager 需要把 `inputs` 传入用户模块，例如 `home-manager.extraSpecialArgs = { inherit inputs; };`。

直接导入本地文件也可以：

```nix
imports = [ /path/to/niri-scratchpad/home-manager.nix ];
```

## 尺寸和行为

初始尺寸在 `scratchpad.py` 的 `DEFAULT_SLOT_SIZE` 中设置。客户端未在 0.5 秒内确认缩放时，不保存旧尺寸，保留待完成状态并在后续呼出／重新聚焦时重试。

已呼出的窗口若被手动改回平铺，重新聚焦只改变焦点，不强制浮动，也不覆盖原模式／几何恢复记录。

954×1028 来自原桌面上的实际显示效果，并非最初补丁里声明的固定尺寸。
显示器可用区域、缩放和应用自身的最小／最大尺寸可能限制实际大小。初次在其他显示器呼出时会重新居中初始化该区域。

`scratch` 是普通工作区，会出现在 Overview 和工作区列表中，也会影响数字工作区索引。这不是真正的最小化或隐藏工作区。

将当前窗口移出暂存管理：

```sh
python3 "${XDG_CONFIG_HOME:-$HOME/.config}/niri/scratchpad.py" release
```

`release` 保留窗口当下的位置、大小和浮动模式，不再在收起时恢复旧值。

成员、顺序、共享区域及恢复信息保存在 `$XDG_RUNTIME_DIR/niri-scratchpad-*.json`，按 Niri socket 区分会话，不上传到仓库。状态文件丢失后可恢复暂存工作区中的成员，但此前保存的原模式／几何无法恢复。

## 检查

```sh
python3 -m unittest discover -s tests -v
niri validate -c scratchpad.kdl
nix-instantiate --parse home-manager.nix >/dev/null
nix flake show --offline --no-write-lock-file
```

39 项回归测试不操作真实桌面窗口，包含自动候选发现、循环、原工作区恢复、来源工作区保留、新开／关闭窗口处理、延迟尺寸确认和手动平铺后的重新聚焦。

另已在实际 Niri 会话中使用隔离的临时窗口验证：共享位置／尺寸切换、居中默认值、原模式／几何恢复、背景布局保持，以及自动借用／循环、原工作区归还和临时工作区名称清理。

来源说明见 [NOTICE.md](NOTICE.md)。
