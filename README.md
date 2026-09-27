# CNKI-Hover · 知网悬浮查询器

> 一个常驻托盘的知网速查器：**全局热键唤出一个极简悬浮搜索条，秒级出结果，回车/单击即读全文。**
> 原生 Windows 桌面应用（PySide6）+ 直调知网接口；登录走**机构校外访问（CARSI / Shibboleth SAML2）**，纯 HTTP 实现，不内嵌浏览器、不走校内 VPN。

---

## 特性

- **登录一次，长期有效**：机构账号登录后，登录态以 Fernet 加密保存在本机（约 30 天），之后静默复用。
- **键鼠双通道**：`↑↓` 移动高亮、`Enter` 打开；鼠标**悬停**即高亮、**左键**打开、**滚轮**像聊天记录一样滑动列表。两条通道驱动同一个高亮项。
- **一点就出来**：结果高亮停留 1 秒即后台预取；真正打开时走本地缓存，首屏通常 < 100 ms。
- **论文式阅读**：正文按版面结构重排 —— 双栏 PDF 正确分栏、剔除页眉页脚、识别摘要/关键词/一二级小节标题、正文首行缩进两字符、两端对齐。
- **原版图像流**：按页渲染机构授权 PDF，支持缩放（20%–300%）、页码跳转、**整页适配窗口**、滚动到哪加载哪（离屏自动释放内存）。
- **三级本地缓存**：题录 / 正文 / 页图，LRU 自动淘汰，容量可配置。

## 系统要求

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10 / 11（x64） |
| 机构 | 已开通**知网校外访问（CARSI）**的高校 |
| 账号 | 学校统一身份认证账号（学号 / 工号 + 密码） |
| Python | 3.11+（源码运行） |

## ⚙️ 零配置：直接选学校就能登录

不需要手写任何配置文件。程序在首次打开登录窗时，会从 **CARSI 联邦**取回全国高校清单
（实测 **7967 所**，含每所学校的 IdP entityID），「机构」输入框**边打边自动补全**：

- 敲 `福建理工` → 自动列出「福建理工大学」，选中即可；
- 登录成功后自动记住你的机构，之后开机即用、长期免登录；
- 清单首次联网获取（约 11MB），随后缓存到本地，之后完全离线可用。

> 完全离线时若缓存为空，可手动在运行目录放 `institutions.json`（模板见
> `institutions.example.json`），键=机构名，值=entityID。

### 凭证文件（可选）

源码运行时支持从 `secrets/account.txt` 读取凭证（该路径已被 `.gitignore` 排除）：

```
机构名称=你的机构
账号=你的学号
密码=你的密码
```

---

## 使用说明

| 动作 | 操作 |
|---|---|
| 唤出 / 收起 | 全局热键（默认 `Alt+Space`，可在设置里改） |
| 移动高亮 | `↑` `↓` 或 鼠标悬停 |
| 打开当前项 | `Enter` 或 鼠标左键 |
| 滑动列表 | **鼠标滚轮**（像聊天记录一样，不改变选中项） |
| 阅读窗内返回 | `Esc`（焦点还给结果列表原来那一行） |
| 切 HTML / 原版 | `Tab` |

**检索语法**：默认按主题模糊检索；也可用「字段:值」前缀 —— `作者:张三`、`篇名:xxx`、`关键词:xxx`、
`摘要:xxx`、`作者单位:xxx`、`文献来源:xxx`、`第一作者:xxx`、`DOI:xxx`（冒号也可以写成句点，如 `作者.张三`）。
更推荐直接用输入框左侧的**下拉框**选检索项，不用手打字段名。

## 从源码运行

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt

# 1. 配置机构（见上文）
copy institutions.example.json institutions.json   # 然后填入你的学校

# 2. 填写凭证（可选，也可以在登录界面输入）
#    secrets/account.txt

# 3. 运行
.venv/Scripts/python.exe run.py
```

打包发行版：

```bash
.venv/Scripts/python.exe tools/make_icon.py
.venv/Scripts/python.exe -m PyInstaller --noconfirm --clean --windowed --onedir \
    --name CNKI-Hover --icon assets/app.ico --paths src \
    --collect-submodules pymupdf --exclude-module tkinter run.py
```

## 目录结构

```
├── run.py                # 入口（python run.py）
├── institutions.example.json  # 机构配置模板（复制为 institutions.json 并填入你的学校）
├── src/
│   ├── cnki_api/         # 知网接口层（可替换解析层）
│   │   ├── auth.py       #   机构校外访问登录（CARSI / Shibboleth SAML2，纯 HTTP）
│   │   ├── search.py     #   检索（kns8s brief/grid）
│   │   └── reader.py     #   取全文 / 页图（机构授权 PDF → 文本 + 图像；双栏分栏提取）
│   └── cnki_hover/       # 应用层
│       ├── main.py       #   装配与生命周期 / 单实例 / 首启登录向导
│       ├── tray.py hotkey.py overlay.py    # 托盘 / 全局热键 / 悬浮窗
│       ├── login_ui.py   #   登录引导
│       ├── result_list.py navigator.py     # 结果列表 / 键鼠状态机
│       ├── prefetch.py reader_window.py page_view.py  # 预取 / 阅读窗 / 原版图像流
│       └── cache.py settings.py theme.py config.py ...  # 缓存 / 设置 / 主题
├── docs/api/             # 三个知网接口的实测文档（login / search / read）
├── tests/                # test_navigator.py：键鼠双通道状态机（离线可跑）
└── tools/make_icon.py    # 生成应用图标
```

运行期数据：开发态在项目 `config/ logs/ outputs/`；打包态在 `%APPDATA%\CNKI-Hover\`。

---

## ⚠️ 合规与使用边界（请务必阅读）

1. **仅限本人机构账号、个人学习使用**。请勿用于批量下载、二次分发、绕过付费墙或任何商业用途。
2. **不要批量下载**。应用按「人工触发 + 低频」设计：对知网请求**间隔 ≥2 秒**、串行、绝不并发。
3. **会消耗机构下载额度**：本项目取全文走的是**机构授权 PDF**（知网在线阅读器的正文接口为前端 SPA，未复现）。
   同一文献有本地缓存、**不会重复下载**；但清空缓存会一并删掉已下载的 PDF，下次阅读需重新取回。
4. **机构订阅边界如实提示**：未订购的库会明确告知「未订阅」，不会假装能取到。
5. **正文不二次分发**：本地缓存仅供本人离线回看。
6. 请遵守中国知网服务条款及所在机构的相关规定。

## License

[MIT](LICENSE)
