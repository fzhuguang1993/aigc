# 测试脚本分类说明

## 📁 文件夹结构

```
test/
├── product/          # 产品图片（1 张）
│   └── product.png
├── kol/             # KOL 明星图片（3 张）
│   ├── kol_1.png
│   ├── kol_2.png
│   └── kol_3.png
├── videos/          # 参考视频（3 个）
│   ├── video_1.mp4
│   ├── video_2.mp4
│   └── video_3.mp4
├── audios/          # 参考音频（3 个）
│   ├── audio_1.mp3
│   ├── audio_2.mp3
│   └── audio_3.mp3
└── scripts/         # 测试脚本
    ├── group1_1+1.txt
    ├── group2_1+1.txt
    ├── group3_1+2.txt
    └── ...
```

## 🎯 测试分组

### Group 1: 产品图 + KOL 图 (1+1)
**目的**: 测试产品图和单个明星的结合效果

#### 脚本 1.1: 产品 + 贾乃亮 1
```
subject_definitions: <Subject 1> is a celebrity endorsement scene featuring the product.
dialogue: 
<d>[Chinese] 大家好，我是贾乃亮。今天给大家推荐这款产品，非常好用！</d>
<d>[English] Hello everyone, I'm Jacky. Today I recommend this product, it's very good!</d>
```

#### 脚本 1.2: 产品 + 贾乃亮 2
```
subject_definitions: <Subject 1> is a celebrity endorsement scene featuring the product.
dialogue: 
<d>[Chinese] 大家好，我是贾乃亮。这款产品我亲测有效，强烈推荐！</d>
<d>[English] Hello everyone, I'm Jacky. This product works great, highly recommend!</d>
```

#### 脚本 1.3: 产品 + 贾乃亮 3
```
subject_definitions: <Subject 1> is a celebrity endorsement scene featuring the product.
dialogue: 
<d>[Chinese] 大家好，我是贾乃亮。用了这款产品，效果真的不一样！</d>
<d>[English] Hello everyone, I'm Jacky. After using this product, the results are amazing!</d>
```

### Group 2: 产品图 + 视频 (1+1)
**目的**: 测试产品图和动态视频的结合效果

#### 脚本 2.1: 产品 + 视频 1
```
subject_definitions: <Subject 1> is a product demonstration scene.
dialogue: 
<d>[Chinese] 看看这个产品的使用效果，非常简单方便！</d>
<d>[English] Look at this product's effect, so simple and convenient!</d>
```

#### 脚本 2.2: 产品 + 视频 2
```
subject_definitions: <Subject 1> is a product usage tutorial.
dialogue: 
<d>[Chinese] 三分钟学会使用这款产品，超级简单！</d>
<d>[English] Learn to use this product in 3 minutes, super easy!</d>
```

#### 脚本 2.3: 产品 + 视频 3
```
subject_definitions: <Subject 1> is a product feature showcase.
dialogue: 
<d>[Chinese] 这款产品的三大核心功能，让你爱不释手！</d>
<d>[English] Three core features of this product that you'll love!</d>
```

### Group 3: 产品图 + 音频 (1+1)
**目的**: 测试产品图和口播音频的结合效果

#### 脚本 3.1: 产品 + 音频 1
```
subject_definitions: <Subject 1> is a voiceover advertisement.
audio_file: audios/audio_1.mp3
dialogue: 
<d>[Chinese] 旁白：这款产品是您的最佳选择！</d>
<d>[English] Voiceover: This product is your best choice!</d>
```

#### 脚本 3.2: 产品 + 音频 2
```
subject_definitions: <Subject 1> is a testimonial ad.
audio_file: audios/audio_2.mp3
dialogue: 
<d>[Chinese] 旁白：用户一致好评，销量领先！</d>
<d>[English] Voiceover: Highly rated by users, sales leader!</d>
```

#### 脚本 3.3: 产品 + 音频 3
```
subject_definitions: <Subject 1> is a promotional announcement.
audio_file: audios/audio_3.mp3
dialogue: 
<d>[Chinese] 旁白：限时优惠，立即购买！</d>
<d>[English] Voiceover: Limited time offer, buy now!</d>
```

### Group 4: 产品图 + KOL 图 + KOL 图 (1+2)
**目的**: 测试多个明星同框的效果

#### 脚本 4.1: 产品 + 贾乃亮 1 + 贾乃亮 2
```
subject_definitions: <Subject 1> is a joint endorsement with multiple celebrities.
dialogue: 
<d>[Chinese] 贾乃亮：这款产品太棒了！</d>
<d>[Chinese] 另一位明星：我也在用，效果很好！</d>
<d>[English] Celebrity 1: This product is amazing! Celebrity 2: I use it too, great results!</d>
```

#### 脚本 4.2: 产品 + 贾乃亮 2 + 贾乃亮 3
```
subject_definitions: <Subject 1> is a team recommendation scene.
dialogue: 
<d>[Chinese] 明星 A：我们团队都在用！</d>
<d>[Chinese] 明星 B：确实好用，值得信赖！</d>
<d>[English] Star A: Our whole team uses it! Star B: Really good, trustworthy!</d>
```

#### 脚本 4.3: 产品 + 贾乃亮 1 + 贾乃亮 3
```
subject_definitions: <Subject 1> is a celebrity duet endorsement.
dialogue: 
<d>[Chinese] 明星甲：强烈推荐这款！</d>
<d>[Chinese] 明星乙：亲测有效，放心购买！</d>
<d>[English] Star A: Highly recommend this! Star B: Tried it myself, safe to buy!</d>
```

### Group 5: 产品图 + 视频 + 视频 (1+2)
**目的**: 测试产品图配合多个视频片段的效果

#### 脚本 5.1: 产品 + 视频 1 + 视频 2
```
subject_definitions: <Subject 1> is a multi-scene product showcase.
dialogue: 
<d>[Chinese] 产品展示：从使用到效果，一目了然！</d>
<d>[English] Product demo: From usage to results, clear at a glance!</d>
```

#### 脚本 5.2: 产品 + 视频 2 + 视频 3
```
subject_definitions: <Subject 1> is a comprehensive tutorial.
dialogue: 
<d>[Chinese] 完整教程：三步搞定产品使用！</d>
<d>[English] Complete tutorial: Master the product in 3 steps!</d>
```

#### 脚本 5.3: 产品 + 视频 1 + 视频 3
```
subject_definitions: <Subject 1> is a highlight reel.
dialogue: 
<d>[Chinese] 精彩集锦：产品亮点全呈现！</d>
<d>[English] Highlights: All product features showcased!</d>
```

### Group 6: 产品图 + 音频 + 音频 (1+2)
**目的**: 测试产品图配合多段音频的效果

#### 脚本 6.1: 产品 + 音频 1 + 音频 2
```
subject_definitions: <Subject 1> is a dual voiceover presentation.
audio_files: [audios/audio_1.mp3, audios/audio_2.mp3]
dialogue: 
<d>[Chinese] 旁白 1：产品介绍</d>
<d>[Chinese] 旁白 2：用户评价</d>
<d>[English] VO 1: Product intro | VO 2: User review</d>
```

#### 脚本 6.2: 产品 + 音频 2 + 音频 3
```
subject_definitions: <Subject 1> is a testimonial combo.
audio_files: [audios/audio_2.mp3, audios/audio_3.mp3]
dialogue: 
<d>[Chinese] 旁白 1：市场反馈</d>
<d>[Chinese] 旁白 2:促销信息</d>
<d>[English] VO 1: Market feedback | VO 2: Promotion info</d>
```

#### 脚本 6.3: 产品 + 音频 1 + 音频 3
```
subject_definitions: <Subject 1> is a mixed media ad.
audio_files: [audios/audio_1.mp3, audios/audio_3.mp3]
dialogue: 
<d>[Chinese] 旁白 1：产品优势</d>
<d>[Chinese] 旁白 2：购买引导</d>
<d>[English] VO 1: Product benefits | VO 2: Purchase guide</d>
```

### Group 7: 产品图 + KOL 图 + 视频 (1+1+1)
**目的**: 测试混合媒体组合效果

#### 脚本 7.1: 产品 + 贾乃亮 1 + 视频 1
```
subject_definitions: <Subject 1> is a celebrity video endorsement.
dialogue: 
<d>[Chinese] 贾乃亮：看我用这款产品的效果！</d>
<d>[English] Jacky: Watch me use this product!</d>
```

#### 脚本 7.2: 产品 + 贾乃亮 2 + 视频 2
```
subject_definitions: <Subject 1> is a tutorial with celebrity.
dialogue: 
<d>[Chinese] 明星演示：手把手教你使用！</d>
<d>[English] Star demo: Step by step tutorial!</d>
```

#### 脚本 7.3: 产品 + 贾乃亮 3 + 视频 3
```
subject_definitions: <Subject 1> is a feature showcase with star.
dialogue: 
<d>[Chinese] 明星讲解：三大功能详解！</d>
<d>[English] Star explains: Three features detailed!</d>
```

### Group 8: 产品图 + KOL 图 + 音频 (1+1+1)
**目的**: 测试明星 + 音频的组合效果

#### 脚本 8.1: 产品 + 贾乃亮 1 + 音频 1
```
subject_definitions: <Subject 1> is a celebrity voiceover ad.
audio_file: audios/audio_1.mp3
dialogue: 
<d>[Chinese] 贾乃亮：这款产品太值得购买了！</d>
<d>[English] Jacky: This product is worth buying!</d>
```

#### 脚本 8.2: 产品 + 贾乃亮 2 + 音频 2
```
subject_definitions: <Subject 1> is a testimonial with celebrity.
audio_file: audios/audio_2.mp3
dialogue: 
<d>[Chinese] 贾乃亮：大家都说好才是真的好！</d>
<d>[English] Jacky: If everyone says it's good, it really is!</d>
```

#### 脚本 8.3: 产品 + 贾乃亮 3 + 音频 3
```
subject_definitions: <Subject 1> is a promotional spot with star.
audio_file: audios/audio_3.mp3
dialogue: 
<d>[Chinese] 贾乃亮：限时特惠，不要错过！</d>
<d>[English] Jacky: Limited special offer, don't miss out!</d>
```

---

## 📝 使用说明

1. **准备素材**:
   - 在 `product/` 放 1 张产品图
   - 在 `kol/` 放 3 张 KOL 图片
   - 在 `videos/` 放 3 个参考视频
   - 在 `audios/` 放 3 个参考音频

2. **运行测试**:
   - 选择对应的脚本编号
   - 上传相应的素材
   - 观察生成的视频效果

3. **评估标准**:
   - 产品清晰度
   - KOL 相似度
   - 音视频同步
   - 整体美观度

---

**状态**: ✅ 测试框架已建立，等待填充实际素材
