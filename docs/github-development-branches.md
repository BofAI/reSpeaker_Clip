# GitHub 开发分支

维护仓库：https://github.com/roger-gan/reSpeaker_Clip 。各阶段按分支保存；
当前设备使用外置补丁分支，HTTPS 原型单独维护，尚未合入当前设备版本。

| 分支 | 用途 |
|---|---|
| [baseline/0.1.2-production](https://github.com/roger-gan/reSpeaker_Clip/tree/baseline/0.1.2-production) | v0.1.2 Production 基线，曾完成录音回归 |
| [experiment/runtime-resources-0.1.2](https://github.com/roger-gan/reSpeaker_Clip/tree/experiment/runtime-resources-0.1.2) | 运行时测量和 AP 失败对照；保留修复前行为 |
| [fix/audio-release-after-stop-0.1.2](https://github.com/roger-gan/reSpeaker_Clip/tree/fix/audio-release-after-stop-0.1.2) | 音频停止释放修复，内置 Wi-Fi 补丁，短测通过 |
| [experiment/external-patch-runtime-0.1.2](https://github.com/roger-gan/reSpeaker_Clip/tree/experiment/external-patch-runtime-0.1.2) | 当前设备版本：音频修复、资源测量、外置补丁、维护工具和实测报告 |
| [experiment/wifi-patch-external-flash](https://github.com/roger-gan/reSpeaker_Clip/tree/experiment/wifi-patch-external-flash) | 早期外置补丁构建实验，保留历史 |
| [experiment/https-upload](https://github.com/roger-gan/reSpeaker_Clip/tree/experiment/https-upload) | HTTPS 上传原型与主机测试，尚未整合当前设备修复／实机验收 |

## 构建记录的提交号对应关系

GitHub 邮箱隐私保护拒绝首次推送后，11 个尚未发布的本地提交改用 GitHub noreply
邮箱重新生成。逐个核对 Git tree 完全相同，代码、提交消息和原始时间不变；
已有远端历史未改写，未强制推送。原始提交保留在本地
`refs/archive/pre-github-email/`，构建归档与设备镜像哈希保持原样。
下表用于将原报告／源码快照中的提交号定位到 GitHub 对应代码。

| 原构建／本地提交 | GitHub 对应提交 |
|---|---|
| `ecc10080b907326d9fb14bb960b1c4bf773e17f5` | [d633d9ca1cf9e927ddf789e006807658ba6528d1](https://github.com/roger-gan/reSpeaker_Clip/commit/d633d9ca1cf9e927ddf789e006807658ba6528d1) |
| `87e89d94623834a69bdd5499c3645f3e60dd7fe6` | [68ce2edfadbd57d3135f550d20cb74f57aa815b2](https://github.com/roger-gan/reSpeaker_Clip/commit/68ce2edfadbd57d3135f550d20cb74f57aa815b2) |
| `4bd8cdfd24a524a3bbf21a0ee89c0059edada7fa` | [b045e98bc038c6ffdd23c64289be4ef7f4453522](https://github.com/roger-gan/reSpeaker_Clip/commit/b045e98bc038c6ffdd23c64289be4ef7f4453522) |
| `faf47a2cb9cfd7a034722877ebb89a3ef1e63035` | [ab73533fcece78a2db00474c1fcff57a1a27ce82](https://github.com/roger-gan/reSpeaker_Clip/commit/ab73533fcece78a2db00474c1fcff57a1a27ce82) |
| `185d2840425cbd53b3b60e38cda40a7f841bd662` | [2f3234587b7439e5b7695e752949d9d50ef61e12](https://github.com/roger-gan/reSpeaker_Clip/commit/2f3234587b7439e5b7695e752949d9d50ef61e12) |
| `98301456decf78de786ff3b1ff9eb5c556ffc859` | [7afc3cc3bde830582b16147b4506181c99e50f70](https://github.com/roger-gan/reSpeaker_Clip/commit/7afc3cc3bde830582b16147b4506181c99e50f70) |
| `05afb8a12e06283709c6dfb1175a9505afca4029` | [7027496fdc5a4a0c4d5941fe2a7ed22bb195c521](https://github.com/roger-gan/reSpeaker_Clip/commit/7027496fdc5a4a0c4d5941fe2a7ed22bb195c521) |
| `c28764ed703b0d07a33f32853d2a19362e8e6f26` | [e1b8e91d96915bb0de6d54c7bb8d898c540feff6](https://github.com/roger-gan/reSpeaker_Clip/commit/e1b8e91d96915bb0de6d54c7bb8d898c540feff6) |
| `5ce11d4adff6c3975fbbf9b4612a4c4d198df314` | [9ba9eeb5620fdf521a2ec31489ec887c74b8c56e](https://github.com/roger-gan/reSpeaker_Clip/commit/9ba9eeb5620fdf521a2ec31489ec887c74b8c56e) |
| `9b4b8787a7d67232c721c044c2afb7781629c104` | [4a7192c62557ef03c2477de724b4c94678399764](https://github.com/roger-gan/reSpeaker_Clip/commit/4a7192c62557ef03c2477de724b4c94678399764) |
| `17681cf01c954794b79b18ce885d5463bfb33563` | [599416342586f574d550977f36eea9fcf3a18c76](https://github.com/roger-gan/reSpeaker_Clip/commit/599416342586f574d550977f36eea9fcf3a18c76) |

## 维护范围

源码、测试和实测报告进入 Git；构建产物、SDK、录音、设备配置／密钥及原始 Flash
备份仅留在本机，不随代码推送。各分支推送不代表合入 `main` 或发布稳定版本。
外置布局回退必须先恢复原始 Flash 区域，再刷回内置补丁应用，详见
[外置补丁实测报告](external-wifi-patch-0.1.2.md)。
