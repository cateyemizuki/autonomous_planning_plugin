# 打包字体说明

本目录打包 [Noto Sans SC](https://fonts.google.com/noto/specimen/Noto+Sans+SC)
（思源黑体 Google 版），供 `/plan list` 日程图片渲染使用，
保证在任何宿主机（裸 Linux / Docker）上排版一致，无需依赖系统中文字体。

| 文件 | 说明 |
|---|---|
| `NotoSansSC-Regular.ttf` | Regular 字重静态实例，子集化后约 2.2MB |
| `OFL.txt` | SIL Open Font License 1.1 协议文本（随字体分发要求） |

- **来源**：google/fonts 仓库的 `NotoSansSC[wght].ttf` 可变字体，
  经 `fonttools varLib.instancer`（wght=400）实例化 +
  `pyftsubset` 子集化（保留 GB2312 全部汉字 / ASCII / 常用中英文标点）。
- **授权**：SIL OFL 1.1，允许随本插件再分发（保留 OFL.txt 即可）；
  该协议要求**不得单独出售字体文件本身**。
- **覆盖范围**：GB2312 常用汉字集（6763 字）+ ASCII + 常用标点，
  覆盖日程名 / 描述的日常中文；极生僻字（如部分人名用字）不在子集内，
  会渲染为缺字方框——遇到时可把 `BUNDLED_FONT_PATH` 指向系统完整字体
  （渲染器会自动回退系统字体链）。
- **加粗**：标题等粗体效果用同色 1px 描边模拟（单一字重文件）。
