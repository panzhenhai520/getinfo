# 汽车行业 RSS 信源 AI 导入清单

**版本：** 1.0
**整理日期：** 2026-08-06
**行业包：** 汽车行业
**用途：** 供 AI 或自动化程序直接建立 RSS 信源、主题路由、来源分级和抓取过滤规则。

> 本文档中的“汽车行业”是一个统一行业包。NVH、主动降噪、风洞、电控仿真、智能驾驶、智能网联、车辆风险、越野性能、具身智能驾驶、eVTOL 等均作为行业包内部主题标签，不应拆成多个行业包。

---

## 0. 给执行 AI 的操作要求

请按以下规则导入：

1. 仅将 `import_mode: direct_rss` 的记录直接添加为 RSS Feed。
2. 对 `import_mode: conditional_rss` 的记录，先执行 HTTP 和 XML 解析测试；测试通过后再启用。
3. 对 `import_mode: rss_directory` 的记录，不得把目录网页误当作 RSS XML；应先从目录页解析真实 Feed，或者作为普通网页信源接入。
4. 不得自行猜测、拼接或编造 RSS 地址。
5. 每个 Feed 导入后必须检查：
   - HTTP 请求成功；
   - Content-Type 为 RSS、Atom、XML 或可解析的 XML；
   - 至少存在一个 `item` 或 `entry`；
   - 标题、原文链接和发布时间可解析；
   - 记录重定向后的最终 URL；
   - 检查最近文章日期，长期不更新的 Feed 标记为 `inactive_suspected`。
6. 遇到 `403`、验证码、登录或付费墙时，不得绕过访问限制；将信源标记为“需人工处理”。
7. RSS 只用于发现文章。正文抓取仍须遵守网站的 robots.txt、使用条款和版权要求。
8. 同一来源的综合 Feed 与专题 Feed 可能重复，必须执行 URL、GUID、规范化标题和正文指纹去重。
9. 所有文章进入同一个“汽车行业包”，再根据 `topic_routes` 添加内部主题标签。
10. 来源权威分不能替代行业关键词门控。没有汽车、车辆、eVTOL 等行业锚点的文章，不得仅因来源权威而直接入库。

---

## 1. 内部主题与优先信源对应表

| 内部主题标签 | 优先 RSS 信源 | 网页搜索补充 | 过滤说明 |
|---|---|---|---|
| NVH、主动降噪、风洞、电控仿真 | IEEE Spectrum Transportation | SAE、ASAM | IEEE Feed 不是汽车专属，必须命中汽车上下文和对应技术词 |
| 智能驾驶、自动驾驶 | Federal Register－NHTSA、IIHS/HLDI、IEEE Spectrum、ACEA | NHTSA、SAE、UNECE WP.29 | 优先保留法规、安全研究和工程技术文章 |
| 智能网联、软件定义汽车 | ACEA、IEEE Spectrum、Federal Register－NHTSA | ASAM、3GPP、UNECE WP.29 | 过滤普通通信、消费电子和非车载软件文章 |
| 车辆风险等级、损伤性维修性 | IIHS/HLDI、Federal Register－NHTSA、NTSB | Thatcham Research、RCAR、GDV | 区分保险风险、碰撞安全、维修成本和事故调查 |
| 新能源汽车、动力电池、热管理 | 工信部、ICCT、ACEA、electrive | SAE、UNECE、企业技术资料 | 媒体信源用于事件发现，法规和技术指标需回到一手来源核验 |
| 越野性能与全地形控制 | IEEE Spectrum、专业汽车媒体 | SAE、中国汽车工程学会、中汽中心 | 必须命中越野、全地形、可通行性、地形识别等词 |
| 具身智能驾驶、世界模型 | IEEE Spectrum | arXiv、SAE、研究机构 | 必须同时命中汽车、车辆、驾驶或自动驾驶上下文 |
| eVTOL和低空飞行器测试 | NTSB、IEEE Spectrum、electrive | FAA、EASA、中国民航相关公开网站 | 区分事故调查、适航测试、试飞和产业事件 |
| 汽车政策、法规和标准 | 工信部、Federal Register－NHTSA、ACEA、ICCT | UNECE WP.29、ISO、SAE、ASAM | 政府法规优先于协会观点和媒体转述 |

---

## 2. 来源等级与证据权重

| 等级 | 来源类型 | 建议证据权重 | 使用原则 |
|---|---|---:|---|
| A1 | 政府监管、官方事故调查 | 5 | 可作为政策、法规、公告和调查事实的一手来源 |
| A2 | 独立研究机构、行业协会、安全研究机构 | 4 | 研究数据可信度较高；协会观点需标记利益相关属性 |
| A3 | 工程技术媒体、专业行业媒体 | 2～3 | 用于技术趋势和事件发现；关键结论应交叉核验 |
| B1 | 消费汽车媒体、车型资讯媒体 | 1～2 | 用于车型、产品和市场事件发现，不作为法规最终依据 |

---

## 3. 便于人工检查的 RSS 信源总表

| ID | 等级 | 来源 | Feed或入口 | 导入方式 | 默认启用 | 主要覆盖 | 当前接入备注 |
|---|---|---|---|---|---:|---|---|
| `miit_rss_directory` | A1 | 工业和信息化部 | `https://www.miit.gov.cn/RRSdy/index.html` | RSS目录/网页 | 否 | 中国汽车政策、公告、新能源汽车、动力电池、智能网联、工业运行数据 | 这是订阅入口页，不是可直接导入的 XML Feed |
| `federal_register_nhtsa` | A1 | Federal Register－NHTSA | `https://www.federalregister.gov/agencies/national-highway-traffic-safety-administration.rss` | 直接RSS | 是 | FMVSS、自动驾驶法规、豁免、不符合项、燃油经济性、安全监管 | 官方机构级 Feed |
| `ntsb_press_releases` | A1 | NTSB Press Releases | 见下方 YAML | 直接RSS | 是 | 道路、自动驾驶、航空、eVTOL相关新闻发布 | 官方 NTSB Feed |
| `ntsb_investigations` | A1 | NTSB Investigations | 见下方 YAML | 直接RSS | 是 | 道路交通、自动驾驶、航空及低空飞行器事故调查 | 官方 NTSB Feed |
| `ntsb_reports` | A1 | NTSB Reports | 见下方 YAML | 直接RSS | 是 | 调查报告、安全研究报告和安全建议相关内容 | 官方 NTSB Feed |
| `acea_main` | A2 | 欧洲汽车制造商协会 ACEA | `https://www.acea.auto/feed/` | 直接RSS | 是 | 欧洲汽车市场、法规、新能源、智能网联、网络安全、产业政策 | 协会观点需标记 |
| `icct_main` | A2 | 国际清洁交通委员会 ICCT | `https://theicct.org/feed/` | 条件RSS | 否 | 新能源汽车、排放、能效、充电、电池和政策评估 | 通用机器人请求可能返回403，先验证再启用 |
| `iihs_hldi` | A2 | IIHS / HLDI | `https://feeds.feedburner.com/iihs` | 条件RSS | 否 | 碰撞安全、ADAS、车型损失率、车辆风险和保险损失 | 旧版FeedBurner地址，须确认近期是否仍更新 |
| `ieee_transportation` | A3 | IEEE Spectrum－Transportation | `https://spectrum.ieee.org/feeds/topic/transportation.rss` | 直接RSS | 是 | 电动汽车、自动驾驶、AI、先进航空器和eVTOL | 非汽车专属，必须做汽车上下文门控 |
| `electrive_main` | A3 | electrive | `https://www.electrive.com/feed/` | 直接RSS | 是 | 电动汽车、电池、充电、商用车、车队、自动驾驶和航空电动化 | 用于事件和趋势发现 |
| `green_car_reports_main` | B1 | Green Car Reports | `https://feeds.highgearmedia.com/?sites=GreenCarReports` | 直接RSS | 是 | 电动车、混合动力、充电、车型发布、召回 | 综合Feed，启用后需与专题Feed去重 |
| `green_car_reports_ev` | B1 | Green Car Reports－纯电专题 | `https://feeds.highgearmedia.com/?sites=GreenCarReports&tags=electric-cars` | 直接RSS | 否 | 纯电车型、电池和充电网络 | 默认关闭，避免与综合Feed大量重复 |
| `edmunds_car_news` | B1 | Edmunds Car News | `https://www.edmunds.com/feeds/rss/car-news.xml` | 直接RSS | 是 | 新车、车型更新、价格和美国市场事件 | 消费媒体，证据权重较低 |
| `edmunds_articles` | B1 | Edmunds Reviews and Articles | `https://www.edmunds.com/feeds/rss/articles.xml` | 直接RSS | 否 | 车型评测、功能体验和消费者视角 | 默认关闭，按需启用 |
| `motor_authority_main` | B1 | Motor Authority | `https://feeds.highgearmedia.com/?sites=MotorAuthority` | 直接RSS | 否 | 新技术车型、性能车、产品事件和谍照 | 产品资讯偏多，默认关闭 |

---

## 4. AI 可直接读取的 YAML 导入配置

> 执行 AI 应优先读取本节。
> `enabled_by_default: true` 不代表可以跳过 Feed 验证，只表示验证成功后默认启用。

```yaml
industry_package:
  id: automotive_industry
  name: 汽车行业
  version: "1.0"
  one_industry_package_only: true

  authority_weights:
    A1_government_or_official_investigation: 5
    A2_research_or_industry_association: 4
    A3_engineering_media: 3
    A3_trade_media: 2
    B1_consumer_automotive_media: 1

  classification_context:
    core_keyword_weight: 3
    extension_keyword_weight: 1
    trend_keyword_weight: 2
    event_keyword_weight: 2
    source_authority_does_not_bypass_industry_gate: true

  global_required_context_any:
    - 汽车
    - 车辆
    - 整车
    - 车载
    - 乘用车
    - 商用车
    - 新能源汽车
    - 智能驾驶
    - 自动驾驶
    - 智能网联
    - eVTOL
    - 电动垂直起降航空器
    - automotive
    - automobile
    - vehicle
    - passenger car
    - commercial vehicle
    - autonomous driving
    - connected vehicle
    - electric vehicle
    - powered-lift

  global_exclude_terms:
    - 汽车报价
    - 经销商优惠
    - 二手车报价
    - 汽车美容
    - 驾校招生
    - 车展美女
    - 股票推荐
    - 概念股推荐
    - 招聘广告

  import_validation:
    follow_redirects: true
    max_redirects: 5
    request_timeout_seconds: 30
    user_agent: "Automotive-Industry-RSS-Collector/1.0 (+contact-email)"
    accept:
      - application/rss+xml
      - application/atom+xml
      - application/xml
      - text/xml
      - text/html
    require_xml_parse_for_direct_rss: true
    require_item_or_entry: true
    require_title: true
    require_link: true
    prefer_guid_for_deduplication: true
    do_not_bypass_403_or_captcha: true
    inactive_check_days: 180

sources:
  - id: miit_rss_directory
    name: 工业和信息化部 RSS订阅入口
    organization: 中华人民共和国工业和信息化部
    country_or_region: CN
    language:
      - zh-CN
    authority_level: A1
    evidence_weight: 5
    source_type: government_regulator
    import_mode: rss_directory
    feed_url: null
    directory_url: "https://www.miit.gov.cn/RRSdy/index.html"
    homepage_url: "https://www.miit.gov.cn/"
    enabled_by_default: false
    topics:
      - automotive_policy
      - regulation_and_standards
      - new_energy_vehicle
      - battery_and_thermal_management
      - intelligent_connected_vehicle
      - automotive_industry_statistics
    required_filter_any:
      - 汽车
      - 车辆
      - 道路机动车辆
      - 新能源汽车
      - 动力电池
      - 智能网联汽车
      - 自动驾驶
      - 汽车工业
    verification_status: directory_page_verified_not_direct_xml
    action:
      - 不得将目录URL直接作为RSS XML导入
      - 尝试从目录页发现真实Feed
      - 若无法发现真实Feed，则作为HTML网页信源接入
      - 抓取后执行汽车关键词二次过滤

  - id: federal_register_nhtsa
    name: Federal Register－NHTSA
    organization: National Highway Traffic Safety Administration
    country_or_region: US
    language:
      - en
    authority_level: A1
    evidence_weight: 5
    source_type: government_regulator
    import_mode: direct_rss
    feed_url: "https://www.federalregister.gov/agencies/national-highway-traffic-safety-administration.rss"
    homepage_url: "https://www.federalregister.gov/agencies/national-highway-traffic-safety-administration"
    enabled_by_default: true
    topics:
      - intelligent_driving
      - vehicle_safety_and_risk
      - regulation_and_standards
      - software_defined_vehicle
      - fuel_economy
    required_filter_any:
      - vehicle
      - motor vehicle
      - automobile
      - automated driving
      - autonomous vehicle
      - ADS
      - FMVSS
      - fuel economy
    verification_status: rss_endpoint_responded_with_rss_content_type

  - id: ntsb_press_releases
    name: NTSB Press Releases
    organization: National Transportation Safety Board
    country_or_region: US
    language:
      - en
    authority_level: A1
    evidence_weight: 5
    source_type: official_investigation
    import_mode: direct_rss
    feed_url: "https://www.ntsb.gov/_layouts/feed.aspx?page=674e62a9-4f3b-4058-846b-150bc1c21aa0&pageurl=%2FPages%2FRSS-Feed-Page.aspx&web=%2F&wp=5c78a16b-edcb-475c-8a9c-93c00783cd61&xsl=1"
    directory_url: "https://www.ntsb.gov/Pages/RSS.aspx"
    enabled_by_default: true
    topics:
      - vehicle_safety_and_risk
      - intelligent_driving
      - evtol_and_low_altitude_aircraft
      - accident_investigation
    required_filter_any:
      - highway
      - vehicle
      - automobile
      - automated driving
      - autonomous
      - electric vehicle
      - aircraft
      - eVTOL
      - powered-lift
    verification_status: direct_feed_link_resolved_from_official_ntsb_directory

  - id: ntsb_investigations
    name: NTSB Investigations
    organization: National Transportation Safety Board
    country_or_region: US
    language:
      - en
    authority_level: A1
    evidence_weight: 5
    source_type: official_investigation
    import_mode: direct_rss
    feed_url: "https://www.ntsb.gov/_layouts/feed.aspx?page=674e62a9-4f3b-4058-846b-150bc1c21aa0&pageurl=%2FPages%2FRSS-Feed-Page.aspx&web=%2F&wp=a19255e2-c8e3-41fd-8c99-f8bc0453cb58&xsl=1"
    directory_url: "https://www.ntsb.gov/Pages/RSS.aspx"
    enabled_by_default: true
    topics:
      - vehicle_safety_and_risk
      - intelligent_driving
      - evtol_and_low_altitude_aircraft
      - accident_investigation
    required_filter_any:
      - highway
      - vehicle
      - automobile
      - automated driving
      - autonomous
      - electric vehicle
      - aircraft
      - eVTOL
      - powered-lift
    verification_status: direct_feed_link_resolved_from_official_ntsb_directory

  - id: ntsb_reports
    name: NTSB Reports
    organization: National Transportation Safety Board
    country_or_region: US
    language:
      - en
    authority_level: A1
    evidence_weight: 5
    source_type: official_investigation
    import_mode: direct_rss
    feed_url: "https://www.ntsb.gov/_layouts/feed.aspx?page=674e62a9-4f3b-4058-846b-150bc1c21aa0&pageurl=%2FPages%2FRSS-Feed-Page.aspx&web=%2F&wp=4d4ae30f-92c9-4e6c-9c58-6bac99822531&xsl=1"
    directory_url: "https://www.ntsb.gov/Pages/RSS.aspx"
    enabled_by_default: true
    topics:
      - vehicle_safety_and_risk
      - intelligent_driving
      - evtol_and_low_altitude_aircraft
      - accident_reports
    required_filter_any:
      - highway
      - vehicle
      - automobile
      - automated driving
      - autonomous
      - electric vehicle
      - aircraft
      - eVTOL
      - powered-lift
    verification_status: direct_feed_link_resolved_from_official_ntsb_directory

  - id: acea_main
    name: ACEA Main Feed
    organization: European Automobile Manufacturers' Association
    country_or_region: EU
    language:
      - en
    authority_level: A2
    evidence_weight: 4
    source_type: industry_association
    import_mode: direct_rss
    feed_url: "https://www.acea.auto/feed/"
    homepage_url: "https://www.acea.auto/"
    enabled_by_default: true
    viewpoint_label: industry_association_view
    topics:
      - automotive_market
      - new_energy_vehicle
      - intelligent_driving
      - intelligent_connected_vehicle
      - software_defined_vehicle
      - automotive_policy
      - cybersecurity
    required_filter_any:
      - automobile
      - automotive
      - vehicle
      - car
      - van
      - truck
      - bus
      - electric vehicle
      - connected vehicle
    verification_status: rss_endpoint_responded_with_rss_content_type

  - id: icct_main
    name: ICCT Main Feed
    organization: International Council on Clean Transportation
    country_or_region: GLOBAL
    language:
      - en
    authority_level: A2
    evidence_weight: 4
    source_type: independent_research
    import_mode: conditional_rss
    feed_url: "https://theicct.org/feed/"
    homepage_url: "https://theicct.org/"
    enabled_by_default: false
    topics:
      - new_energy_vehicle
      - emissions
      - energy_efficiency
      - charging
      - battery_and_thermal_management
      - commercial_vehicle
      - automotive_policy
    required_filter_any:
      - vehicle
      - car
      - truck
      - bus
      - electric vehicle
      - zero-emission vehicle
      - battery
      - charging
    verification_status: feed_url_known_but_generic_bot_received_403
    action:
      - 使用清晰且合规的User-Agent测试
      - 若仍返回403则保持禁用
      - 不得绕过访问控制
      - 可改用官网网页搜索或SerpAPI发现

  - id: iihs_hldi
    name: IIHS / HLDI Feed
    organization: Insurance Institute for Highway Safety / Highway Loss Data Institute
    country_or_region: US
    language:
      - en
    authority_level: A2
    evidence_weight: 4
    source_type: independent_safety_research
    import_mode: conditional_rss
    feed_url: "https://feeds.feedburner.com/iihs"
    homepage_url: "https://www.iihs.org/"
    enabled_by_default: false
    topics:
      - vehicle_safety_and_risk
      - intelligent_driving
      - ADAS
      - crashworthiness
      - insurance_loss
      - repairability_and_damageability
    required_filter_any:
      - vehicle
      - crash
      - collision
      - driver assistance
      - automatic emergency braking
      - insurance loss
      - safety rating
    verification_status: xml_endpoint_responded_but_recent_activity_not_confirmed
    action:
      - 导入前检查最近文章日期
      - 若180天内没有更新则标记inactive_suspected
      - 同时保留IIHS官网网页搜索作为补充

  - id: ieee_transportation
    name: IEEE Spectrum Transportation
    organization: IEEE Spectrum
    country_or_region: GLOBAL
    language:
      - en
    authority_level: A3
    evidence_weight: 3
    source_type: engineering_media
    import_mode: direct_rss
    feed_url: "https://spectrum.ieee.org/feeds/topic/transportation.rss"
    homepage_url: "https://spectrum.ieee.org/transportation"
    enabled_by_default: true
    topics:
      - nvh_anc_wind_tunnel_controls
      - intelligent_driving
      - intelligent_connected_vehicle
      - embodied_intelligent_driving
      - evtol_and_low_altitude_aircraft
      - new_energy_vehicle
    required_filter_any:
      - automotive
      - automobile
      - vehicle
      - car
      - truck
      - autonomous driving
      - connected vehicle
      - electric vehicle
      - eVTOL
      - powered-lift
      - vehicle acoustics
      - automotive wind tunnel
      - hardware-in-the-loop
    verification_status: rss_endpoint_responded_with_rss_content_type

  - id: electrive_main
    name: electrive Main Feed
    organization: electrive
    country_or_region: GLOBAL
    language:
      - en
    authority_level: A3
    evidence_weight: 2
    source_type: professional_trade_media
    import_mode: direct_rss
    feed_url: "https://www.electrive.com/feed/"
    homepage_url: "https://www.electrive.com/"
    enabled_by_default: true
    topics:
      - new_energy_vehicle
      - battery_and_thermal_management
      - charging
      - commercial_vehicle
      - intelligent_driving
      - hydrogen_mobility
      - evtol_and_low_altitude_aircraft
    required_filter_any:
      - electric vehicle
      - EV
      - battery
      - charging
      - electric truck
      - autonomous driving
      - eVTOL
      - electric aviation
    verification_status: rss_endpoint_responded_with_rss_content_type

  - id: green_car_reports_main
    name: Green Car Reports Main Feed
    organization: Green Car Reports
    country_or_region: US
    language:
      - en
    authority_level: B1
    evidence_weight: 2
    source_type: automotive_media
    import_mode: direct_rss
    feed_url: "https://feeds.highgearmedia.com/?sites=GreenCarReports"
    homepage_url: "https://www.greencarreports.com/"
    enabled_by_default: true
    topics:
      - new_energy_vehicle
      - hybrid_vehicle
      - charging
      - vehicle_launch
      - recall
    required_filter_any:
      - electric car
      - electric vehicle
      - hybrid
      - plug-in hybrid
      - charging
      - battery
      - recall
    verification_status: xml_endpoint_responded
    deduplication_group: green_car_reports

  - id: green_car_reports_ev
    name: Green Car Reports Electric Cars Feed
    organization: Green Car Reports
    country_or_region: US
    language:
      - en
    authority_level: B1
    evidence_weight: 2
    source_type: automotive_media
    import_mode: direct_rss
    feed_url: "https://feeds.highgearmedia.com/?sites=GreenCarReports&tags=electric-cars"
    homepage_url: "https://www.greencarreports.com/"
    enabled_by_default: false
    topics:
      - new_energy_vehicle
      - battery
      - charging
    verification_status: xml_endpoint_responded
    deduplication_group: green_car_reports
    action:
      - 仅在不启用综合Feed或系统具备可靠去重时启用

  - id: edmunds_car_news
    name: Edmunds Car News
    organization: Edmunds
    country_or_region: US
    language:
      - en
    authority_level: B1
    evidence_weight: 2
    source_type: consumer_automotive_media
    import_mode: direct_rss
    feed_url: "https://www.edmunds.com/feeds/rss/car-news.xml"
    homepage_url: "https://www.edmunds.com/car-news/"
    enabled_by_default: true
    topics:
      - vehicle_launch
      - model_update
      - automotive_market
      - new_energy_vehicle
      - intelligent_driving_features
    required_filter_any:
      - car
      - vehicle
      - truck
      - SUV
      - electric vehicle
      - hybrid
      - driver assistance
    verification_status: xml_endpoint_responded

  - id: edmunds_articles
    name: Edmunds Reviews and Articles
    organization: Edmunds
    country_or_region: US
    language:
      - en
    authority_level: B1
    evidence_weight: 1
    source_type: consumer_automotive_media
    import_mode: direct_rss
    feed_url: "https://www.edmunds.com/feeds/rss/articles.xml"
    homepage_url: "https://www.edmunds.com/"
    enabled_by_default: false
    topics:
      - vehicle_review
      - consumer_experience
      - vehicle_features
    verification_status: xml_endpoint_responded
    action:
      - 仅在需要消费者体验和车型评测时启用

  - id: motor_authority_main
    name: Motor Authority Main Feed
    organization: Motor Authority
    country_or_region: US
    language:
      - en
    authority_level: B1
    evidence_weight: 1
    source_type: automotive_media
    import_mode: direct_rss
    feed_url: "https://feeds.highgearmedia.com/?sites=MotorAuthority"
    homepage_url: "https://www.motorauthority.com/"
    enabled_by_default: false
    topics:
      - vehicle_launch
      - performance_vehicle
      - automotive_technology
      - prototype_and_spy_shot
    required_filter_any:
      - vehicle
      - car
      - automotive
      - electric vehicle
      - autonomous
      - technology
    verification_status: xml_endpoint_responded

topic_routes:
  - topic_id: nvh_anc_wind_tunnel_controls
    name: NVH、主动降噪、风洞、电控仿真
    preferred_source_ids:
      - ieee_transportation
    required_topic_terms_any:
      - 汽车NVH
      - 整车NVH
      - 汽车主动降噪
      - 主动道路噪声控制
      - 汽车风洞
      - 气动声学
      - 汽车电控仿真
      - 汽车硬件在环
      - 汽车XiL
      - automotive NVH
      - vehicle acoustics
      - active road noise control
      - automotive wind tunnel
      - aeroacoustics
      - hardware-in-the-loop
      - software-in-the-loop
      - model-in-the-loop
    web_search_supplement_ids:
      - sae_web_search
      - asam_web_search

  - topic_id: intelligent_driving
    name: 智能驾驶、自动驾驶
    preferred_source_ids:
      - federal_register_nhtsa
      - iihs_hldi
      - ieee_transportation
      - acea_main
    required_topic_terms_any:
      - 智能驾驶
      - 自动驾驶
      - 组合驾驶辅助
      - 高级驾驶辅助
      - autonomous driving
      - automated driving
      - autonomous vehicle
      - driver assistance
      - ADAS
      - ADS

  - topic_id: intelligent_connected_and_sdv
    name: 智能网联、软件定义汽车
    preferred_source_ids:
      - acea_main
      - ieee_transportation
      - federal_register_nhtsa
    required_topic_terms_any:
      - 智能网联汽车
      - 车路协同
      - 软件定义汽车
      - 汽车电子电气架构
      - connected vehicle
      - C-V2X
      - V2X
      - software-defined vehicle
      - vehicle E/E architecture
      - zonal architecture

  - topic_id: vehicle_risk_and_repairability
    name: 车辆风险等级、损伤性维修性
    preferred_source_ids:
      - iihs_hldi
      - federal_register_nhtsa
      - ntsb_press_releases
      - ntsb_investigations
      - ntsb_reports
    required_topic_terms_any:
      - 车辆风险等级
      - 车型风险等级
      - 车辆可保性
      - 损伤性
      - 维修性
      - 碰撞维修成本
      - Vehicle Risk Rating
      - insurability
      - damageability
      - repairability
      - insurance loss

  - topic_id: new_energy_battery_thermal
    name: 新能源汽车、动力电池、热管理
    preferred_source_ids:
      - miit_rss_directory
      - icct_main
      - acea_main
      - electrive_main
      - green_car_reports_main
    required_topic_terms_any:
      - 新能源汽车
      - 动力电池
      - 电池热管理
      - 整车热管理
      - 热失控
      - 电动汽车
      - electric vehicle
      - EV battery
      - battery thermal management
      - thermal runaway
      - charging

  - topic_id: offroad_and_all_terrain
    name: 越野性能与全地形控制
    preferred_source_ids:
      - ieee_transportation
      - motor_authority_main
    required_topic_terms_any:
      - 越野性能
      - 全地形控制
      - 越野自动驾驶
      - 地形识别
      - 地形可通行性
      - 越野耐久
      - off-road vehicle
      - all-terrain
      - terrain recognition
      - terrain traversability
      - off-road autonomy
    web_search_supplement_ids:
      - sae_web_search

  - topic_id: embodied_intelligent_driving
    name: 具身智能驾驶、世界模型
    preferred_source_ids:
      - ieee_transportation
    required_topic_terms_any:
      - 具身智能驾驶
      - 汽车具身智能
      - 车辆具身智能
      - 驾驶世界模型
      - embodied autonomous driving
      - vehicle embodied intelligence
      - driving world model
      - embodied driving agent
    required_vehicle_context_any:
      - 汽车
      - 车辆
      - 驾驶
      - 自动驾驶
      - vehicle
      - automotive
      - driving
      - autonomous

  - topic_id: evtol_and_low_altitude_aircraft
    name: eVTOL和低空飞行器测试
    preferred_source_ids:
      - ntsb_press_releases
      - ntsb_investigations
      - ntsb_reports
      - ieee_transportation
      - electrive_main
    required_topic_terms_any:
      - eVTOL
      - 电动垂直起降航空器
      - 低空飞行器测试
      - 适航测试
      - 试飞
      - powered-lift
      - Advanced Air Mobility
      - AAM
      - flight test
      - airworthiness certification

  - topic_id: automotive_policy_regulation_standards
    name: 汽车政策、法规和标准
    preferred_source_ids:
      - miit_rss_directory
      - federal_register_nhtsa
      - acea_main
      - icct_main
    required_topic_terms_any:
      - 汽车政策
      - 汽车法规
      - 汽车标准
      - 道路机动车辆
      - FMVSS
      - vehicle regulation
      - automotive regulation
      - vehicle standard
      - type approval

web_search_supplements:
  - id: sae_web_search
    name: SAE International网页搜索
    import_as_rss: false
    queries:
      - "site:sae.org automotive NVH testing"
      - "site:sae.org active road noise control"
      - "site:sae.org automotive wind tunnel"
      - "site:sae.org terrain traversability off-road vehicle"
      - "site:sae.org autonomous driving validation"

  - id: asam_web_search
    name: ASAM网页搜索
    import_as_rss: false
    queries:
      - "site:asam.net XiL automotive testing"
      - "site:asam.net OpenSCENARIO autonomous driving"
      - "site:asam.net OpenDRIVE vehicle simulation"
      - "site:asam.net automotive test automation"

deduplication:
  primary_keys_in_order:
    - normalized_guid
    - canonical_url
    - normalized_title_and_publication_date
    - content_fingerprint
  strip_url_parameters:
    - utm_source
    - utm_medium
    - utm_campaign
    - utm_term
    - utm_content
    - fbclid
    - gclid
  known_overlap_groups:
    - group_id: green_car_reports
      source_ids:
        - green_car_reports_main
        - green_car_reports_ev
  prefer_source_by_authority_weight: true
  preserve_all_source_links_for_same_event: true

storage_policy:
  always_store:
    - source_id
    - source_name
    - authority_level
    - feed_url
    - item_guid
    - title
    - summary
    - published_at
    - updated_at
    - original_url
    - canonical_url
    - language
    - topic_tags
    - trend_or_event_class
    - first_seen_at
    - last_seen_at
  full_text:
    enabled_only_when_site_policy_allows: true
    never_bypass_login_paywall_or_captcha: true
  evidence:
    distinguish_government_research_association_and_media: true
    media_report_is_not_automatically_a_verified_fact: true
```

---

## 5. 建议的首批默认启用清单

验证成功后，第一批启用：

```text
federal_register_nhtsa
ntsb_press_releases
ntsb_investigations
ntsb_reports
acea_main
ieee_transportation
electrive_main
green_car_reports_main
edmunds_car_news
```

暂不默认启用：

```text
miit_rss_directory
原因：目录网页，不是直接RSS XML

icct_main
原因：可能对通用机器人返回403

iihs_hldi
原因：旧版FeedBurner地址，需要确认近期活跃度

green_car_reports_ev
原因：与综合Feed重复较多

edmunds_articles
原因：评测内容较多，非行业情报核心来源

motor_authority_main
原因：产品资讯和谍照比例较高
```

---

## 6. AI 导入后的验收清单

执行 AI 完成信源添加后，应输出以下结果：

```text
1. 成功导入的Feed数量
2. 验证失败的Feed数量
3. 每个失败Feed的HTTP状态和解析错误
4. 每个Feed最新文章的发布时间
5. 疑似长期不活跃的Feed
6. 被识别为RSS目录而未直接导入的地址
7. 发生重定向的Feed及最终URL
8. 已启用的主题标签映射
9. 已配置的去重组
10. 未绕过任何403、验证码、登录或付费墙的确认
```

推荐最终状态值：

```text
active
disabled_by_default
validation_failed
blocked_403
inactive_suspected
directory_only
manual_review_required
```

---

## 7. 重要说明

1. 工信部链接是官方 RSS 订阅入口，但当前文档没有把它伪装成直接 XML Feed。
2. NTSB 已拆成新闻发布、事故调查和报告三条可直接导入的官方 Feed。
3. ICCT 和 IIHS/HLDI 应先做运行时验证，不建议在未检查的情况下强行启用。
4. IEEE Spectrum Transportation 不是汽车专属 Feed，必须经过汽车行业关键词门控。
5. Green Car Reports、Edmunds、Motor Authority 属于补充媒体来源，其证据权重低于政府、官方调查、独立研究和行业协会。
6. SAE 和 ASAM 在本清单中作为网页搜索补充，不得被 AI 编造为 RSS Feed。
7. RSS 免费订阅通常只表示可接收标题、摘要和原始链接，不代表可以自由复制或重新发布全文。
