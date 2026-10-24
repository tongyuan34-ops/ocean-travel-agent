# 首屏背景视频生成提示词

> 用途：生成一段**只做网页首屏背景**的海洋视频。不参与任何交互，自动静音循环。
> 参考构图来自用户提供的原站录屏：低机位贴水面、右侧落日、左侧留白放标题。

---

## 0. 硬性规格（先定参数再写提示词）

| 项 | 值 | 原因 |
|---|---|---|
| 时长 | **10 秒**（8–12 秒均可） | 太短循环痕迹明显，太长文件过大 |
| 画幅 | **16:9** | 首屏全屏铺满，桌面为主 |
| 分辨率 | **1920×1080** | 1080p 足够，4K 会让首屏加载过重 |
| 帧率 | **24fps** | 电影感，且比 30fps 更省码率 |
| 镜头 | **单镜头，一镜到底** | 多镜头无法做无缝循环 |
| 运镜 | **只有一种**：极缓慢前推（约 3–5%） | 多种运镜会让循环接不上 |
| 水印 | **必须无** | 有 watermark 直接废弃重生成 |
| 主体 | 只有海面、天空、太阳 | 不出现人、船、鸟、建筑 |

---

## 1. 主提示词（中文，直接粘贴）

```
生成一段 10 秒、可无缝循环的实拍电影感海洋空镜，用作网站首屏背景。

【场景】开阔海面，黄昏日落时分。天空占画面上方约 60%，海面占下方约 40%，
地平线位于画面高度约 62% 处。太阳落在画面右侧约 72% 的位置，贴近地平线，
发出温暖的金橙色光，海面上有一条由太阳延伸向镜头的细碎金色反光带。
天空有少量薄而透的卷云，被夕阳染成淡橙与浅粉；右上角天空保持通透的淡蓝灰色。

【机位】极低机位，镜头中心离水面约 0.3–0.5 米，仿佛趴在水面上拍摄。
前景有 1–2 道缓慢起伏的浪脊从画面下方掠过，浪尖带细小水花与逆光高光，
但不要遮挡画面左侧区域。中景海面密布细碎波纹，远景海平面平直安静。

【运镜】单一运镜：镜头以极慢的速度向前推进约 4%，同时有极轻微的上下浮动
（幅度不超过画面高度的 1%），模拟漂浮在水面的自然呼吸感。
没有摇镜、没有俯仰、没有变焦、没有航拍升高。

【光线与色彩】自然逆光，暖冷对比。高光为金橙色，暗部海水为深蓝青色。
整体色调克制、通透、真实，参考色：#cfdfee（天空浅蓝灰）、
#f3c99a（夕阳暖光）、#12283f（海水暗部）。

【质感】真实摄影，35mm 电影镜头，f/2.8 浅景深，前景浪花轻微虚化，
胶片颗粒极轻，动态范围自然，不过曝不欠曝。写实、安静、高级，像高端旅行品牌广告。

【循环要求】结尾回到与开头几乎一致的构图与水面状态，便于无缝循环播放。

【必须满足】画面左侧约 45% 的区域保持明亮、低细节、无遮挡，
因为网页会在这个区域叠加深色大标题。
```

---

## 2. 英文版（多数视频模型对英文更稳定，二选一使用）

```
A 10-second seamlessly loopable cinematic ocean shot for a website hero background.

SCENE: Open sea at golden-hour sunset. Sky occupies the upper ~60% of the frame,
ocean the lower ~40%, horizon line at ~62% height. The sun sits at about 72% width,
just above the horizon, casting a warm golden-orange glow with a narrow glitter path
running from the sun toward the camera. A few thin, translucent cirrus clouds tinted
pale orange and soft pink; the upper-right sky stays clean pale blue-grey.

CAMERA: Extremely low angle, lens roughly 0.3–0.5 m above the water surface,
as if lying on the water. One or two slow foreground wave crests drift past the
bottom of frame with tiny backlit spray, never blocking the left side of the image.
Mid-ground water is textured with fine ripples; the distant horizon is calm and level.

MOVEMENT: A single, very slow dolly-in of about 4%, combined with a subtle vertical
float of no more than 1% of frame height — the natural breathing of a camera on water.
No pan, no tilt, no zoom, no drone rise, no handheld shake.

LIGHT & COLOR: Natural backlight, warm-cool contrast. Highlights golden-orange,
shadows deep blue-teal. Restrained, airy, photorealistic palette:
sky #cfdfee, sunset glow #f3c99a, deep water #12283f.

TEXTURE: Real photography, 35mm cinema lens, f/2.8 shallow depth of field,
foreground spray slightly soft, very light film grain, natural dynamic range,
no clipping. Quiet, premium, high-end travel-brand commercial look.

LOOP: The final frame must match the opening composition and water state closely
enough for a seamless loop.

MUST HAVE: The left ~45% of the frame stays bright, low-detail and unobstructed,
because large dark typography will be overlaid there in the web page.
```

---

## 3. 负向提示词 / 禁止项

```
no text, no subtitles, no captions, no watermark, no logo, no UI elements,
no people, no hands, no boats, no ships, no birds, no buildings, no islands,
no underwater shot, no split screen, no collage, no time-lapse, no fast camera,
no drone aerial view, no camera shake, no lens flare circles, no heavy vignette,
no oversaturated HDR, no teal-orange grading extreme, no cartoon, no anime,
no 3D render, no CGI look, no plastic water, no frozen waves, no mirrored sky,
no night scene, no storm, no rain
```

> 如果模型不认负向提示词，就把「禁止项」改写成正面表述：
> 「画面中只有海面、天空、云与太阳；干净的自然场景；真实摄影质感」。

---

## 4. 构图参考（按录屏实测）

```
┌──────────────────────────────────────────────────────┐
│  波浪logo  OCEAN        Discover  Our story  Journal   [Explore now ↗] │
│                                                      │
│  FEEL THE WORLD SLOW DOWN                            │
│  Closer to                                           │
│  the ocean.                           ☀ 太阳 x≈72%   │
│  A moment of stillness.                   y≈55%      │
│  An endless sense of possibility.                    │
│  [Explore the ocean →]  ( ▷ ) Watch the film         │
│ ─────────────────── 地平线 y≈62% ───────────────────  │
│           海面：细碎波纹 + 金色反光带                  │
│  前景浪脊（下方 15%，不侵入左侧文字区）                 │
└──────────────────────────────────────────────────────┘
```

关键数字：

- 地平线：画面高度 **62%**
- 太阳：横向 **72%**，纵向 **55%**，半径约画面高度 3%
- 左侧文字区：横向 **0–45%**，必须在每一帧都保持明亮（平均亮度高于 60%）
- 前景浪脊：只出现在下方 **15%**，且不进入左侧文字区
- 天空/海面比例：约 **6 : 4**

---

## 5. 生成后处理

### 5.1 无缝循环

如果模型输出首尾不完全一致，用 0.4 秒交叉淡化接成循环（需要 ffmpeg）：

```bash
ffmpeg -i raw.mp4 -filter_complex \
 "[0:v]split[a][b];[a]trim=0:9.6,setpts=PTS-STARTPTS[main];[b]trim=9.6:10,setpts=PTS-STARTPTS[tail];[main][tail]xfade=transition=fade:duration=0.4:offset=9.2[v]" \
 -map "[v]" -an -c:v libx264 -crf 22 -pix_fmt yuv420p loop.mp4
```

### 5.2 转成网页用的两种格式

```bash
# WebM / VP9：优先加载，体积最小
ffmpeg -i loop.mp4 -an -c:v libvpx-vp9 -b:v 1.8M -crf 32 -row-mt 1 hero.webm

# MP4 / H.264：Safari 与老安卓兜底
ffmpeg -i loop.mp4 -an -c:v libx264 -profile:v high -b:v 2.4M -pix_fmt yuv420p -movflags +faststart hero.mp4

# 首帧封面（视频加载前显示）
ffmpeg -i loop.mp4 -vf "select=eq(n\,24),scale=1920:-2" -frames:v 1 -q:v 3 poster.jpg
```

没有 ffmpeg 时，用在线转换工具按上面参数导出即可，**务必勾选"去除音轨"**。

### 5.3 文件位置

```
frontend/public/assets/hero/
├─ hero.webm     # VP9，目标 3–5 MB
├─ hero.mp4      # H.264，目标 4–7 MB
└─ poster.jpg    # 1920 宽，质量 82，目标 < 300 KB
```

---

## 6. 验收清单（逐条对照，不合格就重生成）

| # | 检查项 | 判定标准 |
|---|---|---|
| 1 | 无文字水印 | 四角与画面中央均无任何字样 |
| 2 | 左侧可放深色字 | 截 5 帧，左侧 45% 平均亮度 > 60% |
| 3 | 镜头只有前推 | 倍速播放无摇镜、无升高、无变焦 |
| 4 | 无水下镜头 | 全片水面以上 |
| 5 | 无人物船只 | 画面里没有任何人造物 |
| 6 | 循环无跳变 | 首尾帧并排对比，构图与浪形接近 |
| 7 | 体积达标 | webm+mp4 合计 < 12 MB |
| 8 | 色调不脏 | 暗部是蓝青而非灰黑，高光是金橙而非纯白 |
| 9 | 前景不挡字 | 浪花不越过左侧 45% 区域 |
| 10 | 帧率稳定 | 无卡顿、无重复帧跳变 |

---

## 7. 常见失败与修正

| 失败表现 | 原因 | 修正 |
|---|---|---|
| 画面变成航拍俯视 | 提示词出现 "ocean" 但没锁机位 | 强化 "low angle, at water level, horizon at 62%" |
| 出现水下镜头 | 模型把 "ocean" 理解成潜水 | 加 "above water only, no underwater" |
| 左侧太空但一片死白 | 逆光过曝 | 加 "natural exposure, highlights not clipped" |
| 浪太大像风暴 | 缺"平静"约束 | 加 "calm sea, gentle 0.3m swell" |
| 结尾循环跳变 | 运镜幅度太大 | 把前推从 4% 降到 2%，或改用交叉淡化 |
| 画面有塑料感 | 默认 CG 风格 | 加 "shot on 35mm film, photorealistic, natural grain" |

---

## 8. 一句话速用版（急着出片时）

```
黄昏海面，极低机位贴近水面，镜头在右侧三分线处对准落日，
金色逆光在水面拉出细碎反光带；前景一两道小浪缓慢起伏，
镜头极缓慢前推 4% 并轻微上下浮动；天空左侧明亮通透留白，
真实电影摄影质感，35mm，f/2.8，24fps，10 秒，可无缝循环，
无人物、无船只、无水印、无文字、无水下镜头。
```
