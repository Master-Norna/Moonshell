# _masters — 像素化前的平滑母版（工作源，别手改 moonshell）

这里是每帧的 **96×96 平滑版**（已抠底/二值 alpha/底部对齐，仍是平滑渐变上色）。
`assets/moonshell/` 里上线的 **24 色像素版是从这里生成的**，不要直接手改 moonshell。

## 改色数 / 重建整套像素图
```
python tools/pixelize.py            # 预览(不改文件) -> docs/pixelize_preview.png
python tools/pixelize.py --apply    # 用 24 色共享调色板重建 assets/moonshell/ 全套
python tools/pixelize.py --colors 16 --apply   # 想更块状就调低色数
```
调色板是从**整套母版**一起建的，所以新旧帧颜色一致、不会逐帧漂色。

## 加一个新姿势
1. 准备一张该姿势的大图，放进 `docs/`。
2. 切图 + 缩放对齐成 96×96 平滑母版，存进**这个目录**。
3. 跑 `python tools/pixelize.py --apply` → 整套（含新帧）统一重新像素化上线。
4. 在 `pet/sprite_config.py` 的 `OPTIONAL_SPRITES` + `pet/pet_window.py` 的 `SPRITE_MAP` 登记名字，再接行为。
