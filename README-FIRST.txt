CNKI-Hover · 首次使用说明（Windows x64）
================================================

1) 配置你的机构（必做，发布版不含任何学校信息）
   在运行目录新建 institutions.json，内容形如：

       {
         "你的机构名称": "https://idp.example.edu.cn/idp/shibboleth"
       }

   entityID 获取：浏览器打开 https://fsso.cnki.net/Shibboleth.sso/DiscoFeed
   按 Ctrl+F 搜你学校中文名，复制对应条目的 entityID。

   打包版的运行目录是： %APPDATA%\CNKI-Hover\
   （首次双击运行一次后会自动生成该目录与 config.json）

2) 首次运行
   双击 CNKI-Hover.exe，托盘出现图标，并弹出登录窗：
   机构名填你在 institutions.json 里写的名字，账号密码用学校统一身份认证。

3) 日常使用
   全局热键 Alt+Space 唤出悬浮搜索条 -> 输入关键词 -> Enter 或单击打开。
   ↑↓ / 鼠标悬停移动高亮；滚轮滑动列表；Esc 从阅读窗返回；Tab 切 HTML/原版阅读。

4) 合规提醒
   仅限本人机构账号、个人学习使用；请勿批量下载、二次分发或用于商业用途。
   取全文会消耗你机构的下载额度（应用有本地缓存，同一篇不会重复下载）。

问题反馈：https://github.com/I-am-gation/CNKI-Hover/issues
