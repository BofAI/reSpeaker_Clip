# Boa Voice：macOS 首次打开、签名与公证

## 当前 Beta 状态

当前 `1.0.1-beta.2` DMG 使用本机临时签名，未经过 Apple 公证。文件完整性和复制后的签名验证通过，不等于通过 Gatekeeper 分发检查。

当前开发机没有可用 Developer ID 签名身份，用户暂时没有付费 Apple Developer 账号；因此本次没有进行 Developer ID 签名、公证或更换已发布的安装包。

## 自用：首次打开

确认下载来自本仓库 Release，且可核对 SHA256SUMS 后，由安装者本人操作：

1. 将 Boa Voice.app 拖入 Applications，复制完成后推出 DMG。
2. 在应用程序中尝试打开 Boa Voice；出现“Apple 无法验证”提示时点击“完成”。
3. 打开“系统设置 → 隐私与安全性”，向下找到 Boa Voice 被阻止的提示，点击“仍要打开”。
4. 按系统要求验证身份，并在再次出现的提示中点击“打开”。

macOS 会为这个 App 保存例外，之后通常可以双击启动。重新构建或更新后的包仍可能需要再次确认。系统管理员管理的 Mac 可能限制这项操作。

这属于 Apple 官方提供的、针对所选 App 的确认流程；无需关闭系统整体安全保护。系统身份验证由用户在本机完成，密码不要发送到聊天或写进仓库。

## 免费的本机构建方式

已有 Flutter、Xcode、CocoaPods 时，也可以在自己的 Mac 从源码运行：

```sh
bash scripts/run-desktop.sh
```

构建安装见 README-desktop.md。自行构建不等同于已经获得可对外分发的 Developer ID 签名与公证。

## 后续免除未公证拦截的准备

需要可用的 Apple Developer 团队和 Developer ID Application 证书；仅有免费 Apple ID 或普通 Apple Development 证书不满足这里的分发要求。

1. 在 Xcode 的 Apple Accounts 中登录有相应权限的开发者账号。
2. 通过 Apple Developer 的 Certificates 页面或 Xcode 创建 Developer ID Application 证书，确保本机钥匙串包含对应私钥。安装 `.cer` 文件但没有匹配私钥不能签名。
3. 对发布构建启用 Hardened Runtime，并用 Developer ID 签署 App 和所有嵌套原生组件，包含安全时间戳。当前 App 含 Flutter、音频、蓝牙等多种原生框架，需要完整签名后重新验证，不能仅替换最外层签名。
4. 重新检查实际签名的 entitlements：不得包含 `com.apple.security.get-task-allow=true`；保留业务所需的网络、麦克风和蓝牙权限。当前临时签名构建不能直接当作满足公证前提的构建。
5. 通过 Xcode 的 Developer ID/Direct Distribution 分发流程，或 `notarytool` 提交公证，查看 Accepted 结果和公证日志。
6. 对最终发布容器附加公证票据，使用 stapler 验证、Gatekeeper 评估，并在其他 Mac 上做首次安装测试。
7. 新包发布为后续 Beta 版本（例如 `1.0.1-beta.3`），不覆盖旧版本的原始附件与校验值。

不需要上架 App Store。Beta 也可以签名与公证。具体能力和证书创建权限由开发者团队角色决定。

### 公证凭据

有账号后可用 `notarytool store-credentials` 的交互提示把凭据保存到本机钥匙串。不要把密码、API 私钥、p12 文件或证书私钥提交到 Git。后续提交使用钥匙串 profile，而不是在脚本中写密码。

```sh
# 仅在账号和证书准备完成后，由用户在本机执行并输入凭据。
xcrun notarytool store-credentials boa-voice-notary
```

当前打包脚本只负责构建和制作临时签名 DMG，尚未接入经过实测的 Developer ID 自动签名、公证流水线；有证书后再完成该流程并验证。

## 官方参考

- [Apple：在 Mac 上安全地打开 App](https://support.apple.com/zh-cn/102445)
- [Apple：Developer ID 证书](https://developer.apple.com/help/account/certificates/create-developer-id-certificates/)
- [Apple：分发前公证 macOS 软件](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)
- [Apple：自定义公证流程](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow)
- [Flutter：macOS 集成](https://docs.flutter.dev/platform-integration/macos/building)
