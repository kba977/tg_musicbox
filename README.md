## Telegram 音乐盒子
在Telegram上搜索并下载某易云音乐


### 效果展示

![效果展示](./images/music_box_0.png)

在发出我想听XXX(歌曲名)后，bot返回的对话框状态由 搜索中 -> 下载中 -> 发送中 -> 歌曲 自动转换

搜索中
![效果展示](./images/music_box_1.png)

下载中
![效果展示](./images/music_box_2.png)


发送中
![效果展示](./images/music_box_3.png)

发送成功
![效果展示](./images/music_box_4.png)


### 部署到Heroku

1. 第一步，根据[此项目](https://github.com/Binaryify/NeteaseCloudMusicApi)建立音乐API 并部署到heroku。
2. 在 app.py 中修改对应地方的变量，如 机器人Token，上一步中音乐API的地址等。
3. 修改之后即可将本项目一同部署到heroku，即可。

