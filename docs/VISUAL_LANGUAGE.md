# MoonShell 视觉语言

这份文件是角色资源、运行时呈现和 README 展示的共同视觉约束。目标不是增加细节，
而是让较少的动作共享同一个角色身份，并让每个特殊姿势只在有明确原因时出现。

## 唯一 canonical

canonical 由两个已有文件共同定义，但两者分工固定：

| 参考 | 负责定义 | 不负责定义 |
|---|---|---|
| [`assets/_masters/idle.png`](../assets/_masters/idle.png) 与发布帧 [`assets/moonshell/idle.png`](../assets/moonshell/idle.png) | 角色身份、96×96 运行时几何、共享 24 色、脸与兜帽比例、脚线和像素密度 | 新动作的构图草案 |
| [`docs/v2_character_anchor.png`](v2_character_anchor.png) | 更清楚的轮廓、统一姿势比例、道具大小和表情可读性 | 可直接上线的精灵、透明底素材或运行时截图 |

<p align="center">
  <img src="../assets/moonshell/idle.png" width="192"
       alt="当前发布用 idle 像素帧，放大两倍展示">
  <br>
  <sub>身份与运行时几何基准；此处按整数 2× 放大。</sub>
</p>

`v2_character_anchor.png` 的原始 Imagegen 稿是 1536×1024 RGB 图片，棋盘格被烤入背景；
仓库中的版本已移除边缘相连的棋盘底，映射到 Active 共享 24 色，并以 384×256 逻辑网格
按 4× 最近邻放大。它仍只是一张视觉方向板，不能切出后直接上线，也不能作为产品截图。
若两份参考出现冲突，以
`idle.png` 的身份、配色和 96×96 约束为准，以方向板的清晰轮廓和克制构图为改进目标。

canonical 的具体约束：

- 角色是小型月灵：大兜帽、小身体、深靛蓝脸与斗篷、金色月牙帽沿、胸前金色吊坠。
- 当前 `idle` 的非透明范围为 `x=15..80, y=10..91`。动作可以改变外轮廓，但脸中心、
  吊坠、兜帽顶和脚底必须保持可比较的锚点；站立姿势脚底落在 `y=91`。
- 发布帧保持 96×96、共享 24 色和二值 Alpha。主要颜色来自现有角色：夜色
  `#01010e/#020324`、靛蓝 `#1d3482`、金色 `#fccc54/#f2b545`、奶油高光
  `#fdf2ae`、紫色 `#402374/#4a3a7a`。
- 金色只强调帽沿、吊坠、眼睛高光和一个语义道具。减少满身碎星、整圈光效和
  与身体争抢注意力的装饰。
- 像素簇要成块、轮廓先于纹理；不要用更多噪点冒充精细。角色缩到实际运行尺寸后，
  应先看清脸、朝向和手中道具，再看到光效。
- 每个事件只允许一个主要道具或效果。礼物、月光和星晶必须能在没有文字时区分。

## 资源状态

资源状态应由 `pet/sprite_config.py` 的 allowlist 表达。暂停和待重做文件可以继续留在
仓库供对照，但不得被运行时随机抽取，也不得出现在面向用户的预览中。

### Active：29 张

- 核心与表情：`idle`, `blink`, `happy`, `curious`, `sleepy`, `peek`, `notify`,
  `hover`, `wave`, `shy`, `pout`, `sad`, `excited`, `love`, `surprised`, `sleep`,
  `dizzy`, `sit`
- 专注与结果：`read`, `magic`, `star`
- 行走：`walk_left_1..4`, `walk_right_1..4`

左右行走帧是精确水平镜像；后续只维护一侧源帧并派生另一侧，避免双向漂移。

### Paused：11 张

`flame`, `twirl`, `moon`, `dash`, `poof`, `wink`, `look_side`, `yawn`,
`teleport`, `question`, `hide`

这些帧或过度依赖大范围特效，或与 active 动作表达重复。`moon` 还会与桌面角落的
成长月形成第二套“月亮”语言，因此保持暂停。

### Redo：3 张

`gift`, `crystal`, `write`

三张在重制通过前同样不进入运行时。优先顺序为 `gift → crystal → write`：

- `gift`：沿用 canonical 身体比例，只在双手之间放一个小而清楚的月光礼物。
- `crystal`：保留手中星晶，移除包住全身的高密度光环。
- `write`：复用 `read` 的坐姿尺度、书桌高度和光向，只改变手部与书写道具。

## 运行时用法

动作语义要比动作数量少，并保持稳定：

- 日常呼吸与待机使用 `idle/blink/sit/sleepy`；安静不是“没有动画”。
- 鼠标靠近和摸头使用 `curious/happy/shy/love`，特殊结果不参与随机待机。
- `read` 只服务专注；`magic/star` 只服务完成、月光或成长反馈。
- `notify/surprised/sad/dizzy` 只回应明确状态，不作为装饰性随机表演。
- 行走帧只负责步态。窗口位移、帧序和脚底锚点必须保持同一方向。

产品只有一条陪伴链路：

`月灵互动或完成专注 → 既有陪伴记录更新 → 角落月亮产生克制反馈 → 同一份记录出现在手账与今日卡片`

角落月亮不是第二个养成系统。真实月相只决定受光形状；已有月光与累计专注只决定
长期尺寸、光晕和星图。专注进行中可以显示同一对象上的轻量进度环，不增加独立入口、
货币或存档。气泡、托盘、角落月亮、手账和今日卡片必须读取同一份已保存状态；保存
失败时不能先显示成长成功。

## 缩放与渲染

整数缩放规则只约束 96×96 栅格角色；矢量绘制的角落月亮可以正常抗锯齿。

- 角色只允许按物理像素整数倍绘制：紧凑模式 1×，标准模式 2×。
- Qt 高 DPI 下先确定目标物理倍率，再换算逻辑尺寸；不要把 96px 帧拉到任意逻辑宽度。
- 角色绘制关闭平滑变换，使用 nearest-neighbor；同一画面中的所有角色帧倍率一致。
- 今日卡片中的角色建议使用 480×480（96×5），不得使用 510×510 这类非整数倍率。
- README 中若单独放 96px 精灵，只使用 96、192、288 或 384px 的实际栅格版本；
  不用 CSS 把精灵任意拉伸后再截图。
- 运行时截图应在 100% 或明确记录的 DPI 下原尺寸截取，不做二次锐化、平滑或 AI 放大。

## README 与宣传素材

- README 首图讲体验链路，不讲资产数量；不得再用 43 张姿势的拼盘证明“动画丰富”。
- 只展示 active 资源。Paused 和 Redo 只允许出现在开发审计材料中。
- 当前 README 首图 `runtime-preview-v2.png` 必须由真实 Active 像素帧和
  `MoonRenderer` 离线生成，并明确标注“体验构成预览，不是操作系统截图”。
- 风格一致的截图完成后，首图应同时出现月灵、短气泡和角落成长月；第二张再展示
  “陪伴／专注 → 月亮反馈 → 手账记录”的三步关系。
- 旧的角色拼盘和旧日卡预览即使仍保留在 `docs/`，也不应继续嵌入 README。
- 图片替代文本描述用户能看到的关系，不使用“高清”“完美”“统一”等未经人工验收的结论。

## Imagegen 工作提示记录

下面是本次生成使用的可复现工作提示，属于语义记录，不是逐字调用日志。原始调用文本
没有完整保存在工作区，因此不得把这两段描述成原始 prompt 的逐字副本。

### 基础动作板

```text
以 assets/_masters/idle.png 为唯一角色身份母版，并参考 docs/image1.png、
docs/image2.png 的动作板结构，生成一张 3×2 正交角色动作板。

六格必须是同一只小型月灵：深靛蓝身体与斗篷、奶油色面部／月牙符号、金色星屑，
使用统一的 24 色受限像素语言；所有格子的轮廓比例、面部、光向和阴影密度一致。

六格依次为：
1. idle 正面安静站立；
2. 被摸后的开心反应；
3. 阅读；
4. 睡眠；
5. 清晰的侧向步行关键姿势；
6. 递出月光／礼物。

每格角色完整不裁切、等尺度；无文字、无 UI、无额外角色。
```

`docs/image1.png` 和 `docs/image2.png` 是当时的本地结构参考，不是发布依赖。后续在当前
仓库复现时，使用 `assets/_masters/idle.png` 锁定身份，并以
`docs/v2_character_anchor.png` 作为姿势比例与清晰度方向参考。

### 被摸反应修订

```text
仅编辑 3×2 动作板右上角的“被摸反应”格，彻底移除大号人手及手臂。
保留同一角色，用微微下压耳朵、眯眼笑和头顶两三颗小星表现被摸后的余韵。
其余五格、布局、配色、光影与像素处理全部保持不变。
```

原始生成结果为带烤入棋盘格的 RGB 方向板。仓库版本已通过
`tools/pixelize_reference_board.py --apply` 去除边缘背景、映射 Active 共享 24 色，并按
4× 最近邻网格像素化。任何派生帧仍需单格重制、透明背景处理、96×96 规范化、整数倍率
预览和人工并排验收后，才能进入 Redo 或 Active。

### 功能姿势重制记录（经过管线进入 Active）

为统一 `read / magic / star`，生成了一张三姿势工作图，保存在不进入发布包的
`art_requests/reference/v2_feature_pose_reference.png`。原图仍是 1536×1024 RGB，且
棋盘格烤入背景，所以从未直接进入运行时。`tools/prepare_feature_sprites.py` 先移除与
画布边缘相连的中性棋盘底，把三格各自缩放到底线 `y=91` 的 96×96 二值 Alpha 母版；
随后 `tools/pixelize.py --apply` 用全部 29 张 Active 共同生成的 24 色共享色板重建发布
帧。处理后的三张通过 Alpha、布局、联合色板和 1×/2×联系表检查后才替换旧版本。
该次图像生成调用的逐字 prompt 为：

```text
Create a production reference sprite sheet for MoonShell with exactly three equal cells in one horizontal row on a genuinely transparent background (alpha, no checkerboard, no floor, no border, no text). Preserve the exact character identity from the first reference: the same tiny chibi moon spirit, deep indigo hood and body, gold crescent trim, small dark face, gold pendant, same head-to-body ratio, same front-facing camera, same overall height, same baseline, and the same amount of transparent margin. Use the second reference only to improve pose readability; do not enlarge or redesign the character. Cell 1: focused reading, seated or standing with one small open dark-purple book clearly visible, calm eyes. Cell 2: focus completion, one restrained gold-violet magic ring or two small sparkles around the hands, silhouette still clean. Cell 3: receiving/holding one small glowing moonlight star in both hands, clearly readable as a result, no gift box or crystal halo. Keep all three poses equally scaled and aligned. Visual treatment: cohesive with the canonical idle, compact hand-authored pixel-art-ready shapes, limited indigo/gold/cream/purple palette, chunky clusters, clean dark outline, minimal texture, one key prop/effect per pose. Avoid painterly gradients, soft airbrush, dense particle fields, extra moons, duplicate characters, cropped limbs, dramatic lighting, or different costume details.
```
