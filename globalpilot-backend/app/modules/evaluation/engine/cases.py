"""营销评测集。事实只来自知识库里的三份一页摘要。改内容时要换 TEST_SET_VERSION。"""

TEST_SET_VERSION = "marketing-cases-v2"
TEST_SET_NAME = "营销场景评测集"
PLATFORMS = ("tiktok", "instagram", "youtube", "linkedin", "facebook", "x")
REGIONS = ("eu-na", "sea", "jp-kr", "other")
BRANDS = ("Wearables", "Audio", "Smart home")

GARMIN = (
    "产品：Garmin vívoactive 5。事实：AMOLED 屏；智能手表模式最长 11 天，省电模式最长 21 天；"
    "仅 GPS 最长 21 小时，全卫星系统最长 17 小时；全卫星系统并播放音乐最长 8 小时；常亮屏最长 5 天；"
    "5 ATM，手册标注可用于游泳。在 Garmin Connect 内配对，不要只靠手机蓝牙。来电可在手表上接受或拒绝，通话仍用手机。"
    "可记录跑步、骑行、游泳、力量训练，并同步到 Garmin Connect；可查看心率、睡眠、压力和 Body Battery。"
    "心率提醒不能用于疾病诊断。续航随功能变化。语气：具体、留有余地，不把上限说成保证。禁止：诊断疾病、10 ATM、无限续航、医疗器械。"
)
SONY = (
    "产品：Sony WH-1000XM5。事实：Bluetooth 5.2；编码 SBC、AAC、LDAC；AAC 且降噪开启最长 30 小时，"
    "降噪和环境声关闭最长 40 小时；LDAC 且降噪开启最长 26 小时；充电约 3.5 小时；重量约 250 g。"
    "可切换降噪和环境声，支持 Speak-to-Chat、双设备多点连接、触控和有线连接。配套 App 是 Headphones Connect。"
    "充电时无法播放。不防喷水，避免雨水或汗水。恢复出厂设置会删除配对信息。语气：把条件说清楚。禁止：100% 降噪、防水、游泳、无限续航。"
)
TAPO = (
    "产品：TP-Link Tapo C210，仅 EU V1。事实：3MP（2304×1296），H.264，15 fps；水平范围 360°；"
    "红外夜视最远 30 英尺（约 9 米）；2.4 GHz Wi-Fi，支持 RTSP 和 ONVIF；microSD 最高 256 GB，存储卡需另购；"
    "适配器直流 9 V / 0.6 A；工作环境 0–40°C，湿度 10%–90% 且无冷凝。支持实时画面、移动检测通知、双向通话、声光警报、隐私模式。"
    "录像时长是实验室估算，不是固定承诺。不套用其他硬件版本的参数。语气：规格优先，写明版本。禁止：5 GHz、标配存储卡、固定录像天数、绝对无法入侵。"
)

CASES: list[dict] = [
    {"code": "M01", "title": "手表续航图文", "brand_name": "Wearables", "region": "eu-na", "persona": "想少充电的跑步用户", "platforms": ["instagram"], "purchase_channels": "Garmin 官网", "user_request": "写一条 Instagram 图文，介绍 vívoactive 5 的智能手表模式续航和 AMOLED 屏，并说明要在 Garmin Connect 里配对。", "brand_profile": GARMIN, "must_include": ["11 天", "AMOLED", "Garmin Connect"], "forbidden_claims": ["诊断疾病", "10 ATM", "无限续航"]},
    {"code": "M02", "title": "游泳防水短视频", "brand_name": "Wearables", "region": "sea", "persona": "在泳池训练的业余选手", "platforms": ["tiktok"], "purchase_channels": "Garmin 官网", "user_request": "写一条 TikTok 口播，说明 vívoactive 5 是 5 ATM、手册写明可用于游泳，并提醒续航会随功能变化。", "brand_profile": GARMIN, "must_include": ["5 ATM", "游泳", "续航随功能变化"], "forbidden_claims": ["10 ATM", "深潜", "诊断疾病"]},
    {"code": "M03", "title": "企业健康边界", "brand_name": "Wearables", "region": "eu-na", "persona": "公司健康计划负责人", "platforms": ["linkedin"], "purchase_channels": "Garmin 企业渠道", "user_request": "写一条 LinkedIn 帖，向负责人说明手表能看心率、睡眠、压力和 Body Battery，但心率提醒不能用于疾病诊断。", "brand_profile": GARMIN, "must_include": ["Body Battery", "睡眠", "不能用于疾病诊断"], "forbidden_claims": ["诊断疾病", "降低病假", "医疗器械"]},
    {"code": "M04", "title": "GPS 骑行视频", "brand_name": "Wearables", "region": "eu-na", "persona": "周末骑行的用户", "platforms": ["youtube"], "purchase_channels": "Garmin 官网", "user_request": "写 YouTube 标题和简介，讲 vívoactive 5 仅 GPS 最长 21 小时、全卫星系统最长 17 小时，户外活动要等 GPS 就绪再开始。", "brand_profile": GARMIN, "must_include": ["21 小时", "17 小时", "GPS 就绪"], "forbidden_claims": ["无限续航", "厘米级定位", "诊断疾病"]},
    {"code": "M05", "title": "音乐续航短帖", "brand_name": "Wearables", "region": "jp-kr", "persona": "跑步时听歌的用户", "platforms": ["x"], "purchase_channels": "Garmin 官网", "user_request": "用一条 280 字以内的 X 帖说明：全卫星系统并播放音乐最长 8 小时，常亮屏最长 5 天。", "brand_profile": GARMIN, "must_include": ["8 小时", "5 天", "音乐"], "forbidden_claims": ["无限续航", "10 ATM", "诊断疾病"]},
    {"code": "M06", "title": "来电不代替手机", "brand_name": "Wearables", "region": "sea", "persona": "第一次买 Garmin 的用户", "platforms": ["facebook"], "purchase_channels": "授权零售", "user_request": "写一条 Facebook 动态，说明来电可以在手表上接受或拒绝，但通话仍要使用手机，配对要在 Garmin Connect 完成。", "brand_profile": GARMIN, "must_include": ["接受或拒绝", "通话仍用手机", "Garmin Connect"], "forbidden_claims": ["独立通话", "诊断疾病", "eSIM"]},
    {"code": "M07", "title": "省电模式", "brand_name": "Wearables", "region": "jp-kr", "persona": "出差很多的职员", "platforms": ["instagram"], "purchase_channels": "Garmin 官网", "user_request": "写 Instagram 配文，对比智能手表模式最长 11 天和省电模式最长 21 天，语气克制。", "brand_profile": GARMIN, "must_include": ["11 天", "21 天", "省电模式"], "forbidden_claims": ["一个月不用充电", "诊断疾病", "最轻"]},
    {"code": "M08", "title": "运动同步双平台", "brand_name": "Wearables", "region": "eu-na", "persona": "同时跑步和力量训练的用户", "platforms": ["tiktok", "instagram"], "purchase_channels": "Garmin 官网", "user_request": "为 TikTok 和 Instagram 各写一套，说明可以记录跑步、骑行、游泳、力量训练，并同步到 Garmin Connect。", "brand_profile": GARMIN, "must_include": ["力量训练", "Garmin Connect", "5 ATM"], "forbidden_claims": ["诊断疾病", "10 ATM", "专业赛事认证"]},
    {"code": "M09", "title": "经销商配对说明", "brand_name": "Wearables", "region": "other", "persona": "运动手表经销商", "platforms": ["linkedin", "x"], "purchase_channels": "经销商门户", "user_request": "写 LinkedIn 帖和一条 X 短帖，提醒门店演示时要在 Garmin Connect 内配对，不要只打开手机蓝牙。", "brand_profile": GARMIN, "must_include": ["Garmin Connect", "不要只靠手机蓝牙", "5 ATM"], "forbidden_claims": ["现货无限", "诊断疾病", "区域独家"]},
    {"code": "M10", "title": "开箱参数核对", "brand_name": "Wearables", "region": "sea", "persona": "看开箱视频的用户", "platforms": ["youtube", "facebook"], "purchase_channels": "授权零售", "user_request": "写 YouTube 开箱标题简介和一条 Facebook 动态，只核对 AMOLED、5 ATM 和睡眠查看，不要承诺医疗用途。", "brand_profile": GARMIN, "must_include": ["AMOLED", "5 ATM", "睡眠"], "forbidden_claims": ["诊断疾病", "附赠耳机", "10 ATM"]},
    {"code": "M11", "title": "降噪续航图文", "brand_name": "Audio", "region": "eu-na", "persona": "经常飞行的旅客", "platforms": ["instagram"], "purchase_channels": "Sony 官网", "user_request": "写 Instagram 配文，说明 WH-1000XM5 在 AAC 且降噪开启时最长 30 小时，并说明这不是消除全部声音。", "brand_profile": SONY, "must_include": ["30 小时", "AAC", "降噪"], "forbidden_claims": ["100% 降噪", "防水", "无限续航"]},
    {"code": "M12", "title": "通勤环境声", "brand_name": "Audio", "region": "sea", "persona": "每天坐地铁的上班族", "platforms": ["tiktok"], "purchase_channels": "Sony 官网", "user_request": "写 TikTok 口播，讲可以用 NC/AMB 切换降噪和环境声，以及 Speak-to-Chat。不要承诺隔绝全部车厢噪声。", "brand_profile": SONY, "must_include": ["环境声", "Speak-to-Chat", "Bluetooth 5.2"], "forbidden_claims": ["完全隔绝", "防水", "游泳"]},
    {"code": "M13", "title": "多点连接采购", "brand_name": "Audio", "region": "eu-na", "persona": "给团队配耳机的采购", "platforms": ["linkedin"], "purchase_channels": "企业采购", "user_request": "写 LinkedIn 帖，说明 WH-1000XM5 支持双设备多点连接、有线连接，配套 App 是 Headphones Connect，重量约 250 g。", "brand_profile": SONY, "must_include": ["多点连接", "Headphones Connect", "250 g"], "forbidden_claims": ["格莱美", "防水", "100% 降噪"]},
    {"code": "M14", "title": "编码续航差异", "brand_name": "Audio", "region": "jp-kr", "persona": "在意音质编码的用户", "platforms": ["youtube"], "purchase_channels": "Sony 官网", "user_request": "写 YouTube 标题和简介，对比 AAC 降噪开启最长 30 小时和 LDAC 降噪开启最长 26 小时。", "brand_profile": SONY, "must_include": ["30 小时", "26 小时", "LDAC"], "forbidden_claims": ["音质世界第一", "防水", "无限续航"]},
    {"code": "M15", "title": "重量与充电短帖", "brand_name": "Audio", "region": "eu-na", "persona": "已经在看这款耳机的用户", "platforms": ["x"], "purchase_channels": "Sony 官网", "user_request": "用一条 280 字以内的 X 帖写重量约 250 g、充电约 3.5 小时，并说明充电时无法播放。", "brand_profile": SONY, "must_include": ["250 g", "3.5 小时", "充电时无法播放"], "forbidden_claims": ["边充边听", "防水", "打折"]},
    {"code": "M16", "title": "不防汗说明", "brand_name": "Audio", "region": "sea", "persona": "运动时也想用耳机的人", "platforms": ["facebook"], "purchase_channels": "授权零售", "user_request": "写 Facebook 动态，明确 WH-1000XM5 不防喷水，要避免雨水和汗水，降噪关闭时 AAC 最长 40 小时。", "brand_profile": SONY, "must_include": ["不防喷水", "汗水", "40 小时"], "forbidden_claims": ["IP 防水", "游泳", "运动耳机认证"]},
    {"code": "M17", "title": "旅行双平台", "brand_name": "Audio", "region": "eu-na", "persona": "自己订机票的旅客", "platforms": ["instagram", "tiktok"], "purchase_channels": "Sony 官网", "user_request": "为 Instagram 和 TikTok 各写一套旅行场景，讲 AAC 降噪开启最长 30 小时，以及可以有线连接。", "brand_profile": SONY, "must_include": ["30 小时", "有线连接", "Bluetooth 5.2"], "forbidden_claims": ["无限续航", "赠送转接头", "100% 降噪"]},
    {"code": "M18", "title": "恢复出厂提醒", "brand_name": "Audio", "region": "other", "persona": "耳机品类买手", "platforms": ["linkedin"], "purchase_channels": "经销商门户", "user_request": "写一条 LinkedIn 帖，提醒买手：恢复出厂设置会删除配对信息，App 名称是 Headphones Connect。", "brand_profile": SONY, "must_include": ["删除配对信息", "Headphones Connect", "Bluetooth 5.2"], "forbidden_claims": ["保证独家", "防水", "获奖"]},
    {"code": "M19", "title": "关闭降噪的续航", "brand_name": "Audio", "region": "eu-na", "persona": "在家办公的人", "platforms": ["youtube"], "purchase_channels": "Sony 官网", "user_request": "写 YouTube 标题和简介，说明降噪和环境声都关闭时 AAC 最长 40 小时，重量约 250 g。", "brand_profile": SONY, "must_include": ["40 小时", "250 g", "环境声"], "forbidden_claims": ["任何房间都适用", "防水", "替代声学处理"]},
    {"code": "M20", "title": "触控与短帖", "brand_name": "Audio", "region": "jp-kr", "persona": "喜欢浅色设备的通勤者", "platforms": ["x", "instagram"], "purchase_channels": "Sony 官网", "user_request": "为 X 写 280 字以内的短帖，并为 Instagram 写配文，介绍触控操作和 Speak-to-Chat，不要写新配色或折扣。", "brand_profile": SONY, "must_include": ["触控", "Speak-to-Chat", "250 g"], "forbidden_claims": ["新配色", "打折", "降噪升级"]},
    {"code": "M21", "title": "云台与夜视", "brand_name": "Smart home", "region": "eu-na", "persona": "想看清玄关的住户", "platforms": ["instagram"], "purchase_channels": "TP-Link 官网", "user_request": "写 Instagram 配文，介绍 Tapo C210 EU V1 的 360° 云台和红外夜视最远约 9 米，并标明只适用于 EU V1。", "brand_profile": TAPO, "must_include": ["360°", "9 米", "EU V1"], "forbidden_claims": ["5 GHz", "绝对无法入侵", "其他硬件版本"]},
    {"code": "M22", "title": "隐私模式口播", "brand_name": "Smart home", "region": "eu-na", "persona": "在意隐私的租房者", "platforms": ["tiktok"], "purchase_channels": "TP-Link 官网", "user_request": "写 TikTok 口播，说明 C210 有隐私模式，本地录像用 microSD，卡要另购，最高 256 GB。不要承诺无法被入侵。", "brand_profile": TAPO, "must_include": ["隐私模式", "microSD", "256 GB"], "forbidden_claims": ["标配存储卡", "绝对无法入侵", "免费云存储"]},
    {"code": "M23", "title": "安装商协议", "brand_name": "Smart home", "region": "eu-na", "persona": "住宅弱电安装商", "platforms": ["linkedin"], "purchase_channels": "安装商门户", "user_request": "写 LinkedIn 帖，告诉安装商 C210 EU V1 支持 RTSP 和 ONVIF，只适用于 2.4 GHz Wi-Fi。", "brand_profile": TAPO, "must_include": ["RTSP", "ONVIF", "2.4 GHz"], "forbidden_claims": ["5 GHz", "兼容所有录像机", "区域独家"]},
    {"code": "M24", "title": "供电与环境", "brand_name": "Smart home", "region": "sea", "persona": "住公寓的远程办公者", "platforms": ["youtube"], "purchase_channels": "授权零售", "user_request": "写 YouTube 标题和简介，说明适配器是直流 9 V / 0.6 A，工作温度 0–40°C，不要把它接到别的电源规格上。", "brand_profile": TAPO, "must_include": ["9 V", "0–40°C", "EU V1"], "forbidden_claims": ["任意电源", "户外防水", "5 GHz"]},
    {"code": "M25", "title": "分辨率短帖", "brand_name": "Smart home", "region": "jp-kr", "persona": "在意画质的小户型住户", "platforms": ["x"], "purchase_channels": "TP-Link 官网", "user_request": "用一条 280 字以内的 X 帖写 3MP（2304×1296）、H.264、15 fps，并注明 EU V1。", "brand_profile": TAPO, "must_include": ["3MP", "15 fps", "EU V1"], "forbidden_claims": ["4K", "5 GHz", "固定录像天数"]},
    {"code": "M26", "title": "移动检测通知", "brand_name": "Smart home", "region": "eu-na", "persona": "经常不在家的家庭用户", "platforms": ["facebook"], "purchase_channels": "TP-Link 官网", "user_request": "写 Facebook 动态，说明支持实时画面、移动检测通知和双向通话，录像时长只是实验室估算。", "brand_profile": TAPO, "must_include": ["移动检测", "双向通话", "实验室估算"], "forbidden_claims": ["固定录像天数", "绝对无法入侵", "标配存储卡"]},
    {"code": "M27", "title": "存储卡另购", "brand_name": "Smart home", "region": "sea", "persona": "第一次装摄像头的租客", "platforms": ["instagram"], "purchase_channels": "授权零售", "user_request": "写 Instagram 配文，把“存储卡需另购、最高 256 GB”写清楚，并说明有声光警报。", "brand_profile": TAPO, "must_include": ["另购", "256 GB", "声光警报"], "forbidden_claims": ["标配存储卡", "户外防水", "5 GHz"]},
    {"code": "M28", "title": "只支持 2.4G", "brand_name": "Smart home", "region": "eu-na", "persona": "第一次安装的屋主", "platforms": ["tiktok", "youtube"], "purchase_channels": "TP-Link 官网", "user_request": "为 TikTok 和 YouTube 写安装说明：C210 EU V1 只写了 2.4 GHz Wi-Fi，不要说它支持 5 GHz。", "brand_profile": TAPO, "must_include": ["2.4 GHz", "EU V1", "Wi-Fi"], "forbidden_claims": ["5 GHz 可用", "无需 Wi-Fi", "兼容所有路由器"]},
    {"code": "M29", "title": "版本不要混用", "brand_name": "Smart home", "region": "other", "persona": "物业设施经理", "platforms": ["linkedin", "facebook"], "purchase_channels": "企业销售", "user_request": "写 LinkedIn 和 Facebook 文案，强调本页参数只对应 C210 EU V1，不能套用其他硬件版本。", "brand_profile": TAPO, "must_include": ["EU V1", "不套用其他硬件版本", "ONVIF"], "forbidden_claims": ["全系列通用", "政府补贴", "绝对无法入侵"]},
    {"code": "M30", "title": "夜视与隐私三平台", "brand_name": "Smart home", "region": "sea", "persona": "一楼住户", "platforms": ["x", "instagram", "tiktok"], "purchase_channels": "授权零售", "user_request": "为 X、Instagram、TikTok 各写一套：红外夜视最远约 9 米，有隐私模式。X 必须短于 280 字。不要承诺 100% 预警。", "brand_profile": TAPO, "must_include": ["9 米", "隐私模式", "360°"], "forbidden_claims": ["100% 预警", "自动关水", "5 GHz"]},
]


def validate_catalog(cases: list[dict] | None = None) -> list[str]:
    rows = CASES if cases is None else cases
    errors: list[str] = []
    codes = [row["code"] for row in rows]
    if len(rows) != 30:
        errors.append(f"需要 30 条，当前 {len(rows)} 条")
    if len(codes) != len(set(codes)):
        errors.append("用例编号重复")
    required = (
        "code",
        "title",
        "brand_name",
        "region",
        "persona",
        "platforms",
        "user_request",
        "brand_profile",
        "must_include",
        "forbidden_claims",
    )
    for row in rows:
        missing = [key for key in required if not row.get(key)]
        if missing:
            errors.append(f"{row.get('code')}: 缺少 {', '.join(missing)}")
        if row.get("brand_name") not in BRANDS:
            errors.append(f"{row.get('code')}: 品牌不在目录中")
        if row.get("region") not in REGIONS:
            errors.append(f"{row.get('code')}: 市场无效")
        platforms = row.get("platforms") or []
        if any(platform not in PLATFORMS for platform in platforms):
            errors.append(f"{row.get('code')}: 平台无效")
        if len(platforms) != len(set(platforms)):
            errors.append(f"{row.get('code')}: 平台重复")
        if len(row.get("persona") or "") > 255:
            errors.append(f"{row.get('code')}: 人群超过 255 字")
    return errors
