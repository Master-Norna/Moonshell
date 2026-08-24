# _masters — 像素化前的平滑母版（工作源，别手改 moonshell）

这里保存角色帧的 **96×96 平滑母版**（已抠底、二值 Alpha、底部对齐，仍是
平滑渐变上色）。`assets/moonshell/` 中启用的 24 色像素帧由这里生成；不要直接
手改生成结果。角色身份、资源分组和验收边界见
[`docs/VISUAL_LANGUAGE.md`](../../docs/VISUAL_LANGUAGE.md)。

## 改色数 / 重建 Active 像素帧

```
python tools/pixelize.py            # 预览(不改文件) -> docs/pixelize_preview.png
python tools/pixelize.py --apply    # 用 24 色共享调色板重建 Active 集合
python tools/pixelize.py --colors 16 --apply   # 想更块状就调低色数
```

调色板只从 `pet/sprite_config.py` 的 `ACTIVE_SPRITES` 建立。Paused / Redo 母版
保留作对照，但不会影响共享色板，也不会被 `--apply` 重建或重新带回运行时。

若图像生成器返回烤入的浅色棋盘底三姿势工作图，先用专用预处理生成联系表并对齐母版，
再运行共享像素化；不要把 RGB 生成图直接改名放进运行目录：

```powershell
python tools/prepare_feature_sprites.py --sheet <三姿势工作图.png>
python tools/prepare_feature_sprites.py --sheet <三姿势工作图.png> --apply
python tools/pixelize.py --apply
```

## 新增或重做姿势

1. 先确认现有 Active 动作不能表达该事件；不要为了数量增加近义姿势。
2. 以 `idle.png` 锁定角色身份，以 `docs/v2_character_anchor.png` 参考轮廓和动作清晰度。
3. 把结果处理为透明、底部对齐的 96×96 平滑母版，存进本目录。
4. 先列入 Redo 并做 1× / 2× 联系表人工复核；通过后才移入 Active allowlist。
5. 运行 `python tools/pixelize.py --apply`，再执行完整的精灵、布局和测试检查。

方向板是 RGB 棋盘底图片，不能直接切出上线；重做帧也不得绕过
`pet/sprite_config.py` 的 Active / Paused / Redo 状态。
