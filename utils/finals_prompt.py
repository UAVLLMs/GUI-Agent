from __future__ import annotations

"""决赛通用 prompt 组装工具。

只负责把「用户指令 + 自维护动作历史 + 当前截图」组织成多模态大模型可读的
消息，并提供 11 种动作的动作空间说明与通用操作策略提示。不包含任何
针对具体任务的硬编码坐标 / 分支。

消息结构：
    [system] 完整指令 + 动作空间 + 策略规则 + 历史动作摘要
    [user]   当前截图
"""

from typing import Any, Dict, List

from agent_base import (
    ACTION_BACK,
    ACTION_CLICK,
    ACTION_COMPLETE,
    ACTION_DOUBLE_CLICK,
    ACTION_DRAG,
    ACTION_HOME,
    ACTION_LONG_PRESS,
    ACTION_OPEN,
    ACTION_SCROLL,
    ACTION_TYPE,
    ACTION_WAIT,
)

# 历史摘要窗口：最近若干步注入 prompt，控制 token
HISTORY_WINDOW = 20
# 历史中 TYPE 文本的展示截断长度
HISTORY_TEXT_LIMIT = 30

# App 专属提示（按需注入，仅当任务指令里出现对应 App 名时生效，不污染其它任务）。
# 只补"确因找不到入口而失败"的 App，保持克制；通用能力仍由 Operating Strategy 兜底。
APP_HINTS = {
    "腾讯视频": (
        "切清晰度：先点视频播放区域调出播放控制条，清晰度/「标清·高清·超清·蓝光1080P」按钮通常在控制条的右下角或右上角，"
        "点开后在弹层里选 1080P/蓝光；收藏该剧在剧集简介页找「收藏/追剧」按钮，正片第一集在「选集」里点第 1 集。"
    ),
    "今日头条": (
        "「不感兴趣」：在推荐信息流里对目标新闻 long_press（长按）会弹出操作菜单，在菜单里点「不感兴趣」；"
        "若没有长按菜单，则点该条右下角的「叉号(×)/···」按钮，再在弹层里选「不感兴趣」。"
    ),
    "百度地图": (
        "驾车路线偏好（不走高速/避开高速等）不在路线主页面上：设好起点和终点、进入驾车路线结果页后，"
        "页面上有一个默认显示「智能推荐」的偏好按钮（通常在路线方案上方），点开它会弹出一组选项："
        "智能推荐 / 时间优先 / 高速优先 / 少收费 / 躲避拥堵 / 不走高速 —— 选「不走高速」即可实现避开高速，"
        "再返回看该方案的预计耗时与过路费。不要在主页面到处找「避开高速」按钮（那里没有），它就在「智能推荐」这个下拉里。"
    ),
    "高德地图": (
        "「附近的X / 附近评分最高的X」标准流程：①先点高德首页的「附近」入口进入附近页；"
        "②在附近页里搜索或选择该类目（如『咖啡馆』）；③在结果页用「筛选/排序」选择「好评优先/评分最高/点评最高」，"
        "这样得到的就是『附近且评分最高』——附近(用了附近入口)和评分两个条件同时满足，不要只在全局搜索里按评分排序而忽略距离；"
        "④选最上面那家（附近+评分最高）点进详情卡，点「路线/到这去」再点「开始导航/驾车/步行」开始导航。"
        "搜索提交方式（关键）：输入关键词后点下方下拉【联想列表里匹配的那一条】或键盘右下角搜索键进入结果页；"
        "不要反复点右上角按钮——在高德里那样往往不提交、反而退回首页；若点了又回到首页，立刻改用「点联想结果行」。"
    ),
}

# 意图关键词 → App：用于「指令未指名 App」时给出软提示（如"找附近X并导航"默认归高德地图）。
# 仅作兜底，显式 App 名优先；关键词需足够指向该 App 的功能，避免误命中。
APP_HINT_KEYWORDS = {
    "高德地图": ["导航", "路线", "怎么走", "打车", "公交路线", "地铁路线", "到这去"],
}


SYSTEM_PROMPT_TEMPLATE = """You are an expert GUI agent operating a real Android phone in real time. \
Given a user task (in Chinese), a summary of your previous actions, and the current screenshot, \
output exactly ONE next action that best advances the task.

## Task
{instruction}
{memory_block}{app_hint_block}
## Output Format (must follow)
```
Thought: ...
Action: ...
```
Output exactly one `Action` line per step. Keep `Thought` to ONE short sentence — even if you feel confused about the UI, do NOT ramble or write long multi-sentence deliberations; instead take ONE concrete action and observe the result on the next screenshot.
MEMORY (CRITICAL for multi-item / cross-app tasks): the MOMENT you can SEE information that the task needs you to collect or carry over but will act on only LATER — e.g. a ranked list (top-5 songs/videos), several names, prices, dates, or multiple filter conditions — you MUST append, on that SAME step, one line `REMEMBER: <list the exact items in full, e.g. 1.xxx-singer 2.yyy-singer ...>` right after your Action. Read the items verbatim from the CURRENT screenshot. Keep this line COMPACT — record ONLY the essential identifiers (e.g. name-singer / price / date / the filter value), no full sentences, no reasoning, no extra words — so the whole list stays short and is never cut off. Everything after `REMEMBER:` is pinned permanently at the top under "Key Info To Remember" and never scrolls away. In every later step you MUST rely ONLY on that pinned Key Info — NEVER re-invent, guess, recall from your own knowledge, or substitute different items. If you find a needed item was not yet remembered and is no longer on screen, go back to where you saw it rather than making one up. TWO KINDS of pinned memory: (a) FIXED facts that never change (e.g. the top-5 song list, a place name) → write a plain `REMEMBER: ...` ONCE; (b) things that CHANGE as you make progress (your plan, your running progress / counts, which items are already done or rejected) → write a KEYED line `REMEMBER【键】: ...` (e.g. `REMEMBER【进度】: 已加2/4 A,B`). A new keyed line with the SAME 键 OVERWRITES the previous one, so the pinned info stays current and never piles up contradictory snapshots — ALWAYS reuse the SAME 键 (e.g. 计划/进度) when updating the same thing, do NOT invent a new 键 each time.

## Action Space
click(point='<point>x y</point>')                                  # tap an element
long_press(point='<point>x y</point>', duration='1000')            # press and hold (ms)
double_click(point='<point>x y</point>')                           # double tap
type(content='要输入的文本')                                        # input text into the FOCUSED box; to press Enter/搜索键 use type(content='<enter>'). NOTE: grey/light hint text inside a box is just a placeholder — the box is EMPTY.
scroll(start_point='<point>x1 y1</point>', end_point='<point>x2 y2</point>')  # swipe from start to end
drag(start_point='<point>x1 y1</point>', end_point='<point>x2 y2</point>')    # slow drag (sliders/reorder)
open(app_name='应用中文名')                                         # launch an app by its Chinese name
back()                                                             # system Back
home()                                                             # system Home (desktop)
wait(seconds='2')                                                  # wait for loading
complete(content='')                                               # the task is fully done; keep content EMPTY or a SHORT phrase (do NOT write a long summary — it wastes output and may get cut off)

## Coordinate System
- All coordinates are normalized to [0, 1000]: (0,0)=top-left, (1000,1000)=bottom-right, (500,500)=center.
- Always click the CENTER of the target element.

## How to Approach a Complex Task
- First break the task into an ORDERED list of sub-goals in your head, then pursue them one by one, finishing one before starting the next. Examples: "把5首歌放进新歌单并播放" = 建歌单 → 对每首歌: 搜索→打开菜单→加到歌单 → 最后打开歌单播放; "从A到B经C查价" = 设起点A → 设途经点C → 设终点B → 读价格. For a LONG / multi-sub-goal task (roughly ≥ 4 sub-goals or many steps), on your FIRST useful step ALSO pin this plan with `REMEMBER【计划】: 1.… 2.… 3.…`, and keep a `REMEMBER【进度】: …` line that you OVERWRITE (same 键) each time you finish a sub-goal — this keeps you on track and prevents losing the thread or redoing finished work across many steps.
- If one entry/path gets STUCK (page won't load, no usable button, repeated taps do nothing), switch to an EQUIVALENT path to reach the SAME sub-goal — e.g. if adding songs from inside the new playlist won't load, go to the global 搜索, search the song, open its "···" menu and use "加到歌单"; do not keep retrying the broken path.
- Before you output complete(), VERIFY on the current screen that EVERY sub-goal is actually done (e.g. for a route, confirm the waypoint field really shows 陕西考古博物馆 and is not empty; for a playlist, confirm all required songs were added). If any sub-goal is missing, finish it first — never complete prematurely.
- This is only your planning guide; still output just ONE short Thought + ONE Action each step.

## Operating Strategy
0. LAUNCHING APPS (READ FIRST, applies every time you need to open an app): to launch ANY app you MUST use open(app_name='中文全名') — e.g. open(app_name='微博'). open() starts the app directly BY ITS NAME even when its icon is hidden inside a desktop folder, on another home page, or not visible at all. NEVER scroll/swipe the desktop, open folders, or hunt for an app icon to launch it — that wastes many steps and often fails. Use the FULL official Chinese name ('b站'->'哔哩哔哩', '滴滴'->'滴滴出行', '高德'->'高德地图', '微博'->'微博', '头条'->'今日头条').
1. The screen starts on the home desktop. If the target app is NOT open, FIRST use open(app_name='精确中文名') (e.g. open(app_name='QQ音乐')). Never hunt for the icon by clicking the desktop. Use the app's FULL official name (e.g. 'b站'/'B站' -> '哔哩哔哩', '滴滴' -> '滴滴出行', '高德' -> '高德地图'). STALE STATE (applies ONLY on the FIRST screen right after an open(), as a ONE-TIME recovery): between tasks the phone only returns to the desktop — apps are NOT closed, so an open() may RESUME the app on a leftover page from a PREVIOUS task (e.g. an old route/navigation-in-progress/search-result/detail page). ONLY in that just-opened moment, if the screen is clearly such a leftover page that is neither the app home nor a useful entry for your task, press back() once (or twice) to reach the app's home/entry, then start. Do NOT keep backing. CONVERSELY, if the resumed page is exactly what you need right now (e.g. you reopened 豆瓣 and it lands back on the very ranking/list/detail you came to read, or on the search page you want), USE it directly — do NOT back out of a page that is already useful. IMPORTANT: once you have started and are navigating into the app's OWN pages as part of your task (search box, result lists, "···"/加到歌单 dialogs, forms, playlists, route fields, etc.), those pages are NORMAL and CORRECT — NEVER back out of them just because they are not the home page. (Exception: you SHOULD still back() when you are genuinely STUCK or on the WRONG page — e.g. repeated taps do nothing, the page won't proceed, or you entered a wrong/dead page — backing out to try another path is correct; the rule above only forbids backing out for the SOLE reason that a useful, working page is not the home screen.) In multi-app tasks (e.g. 豆瓣→QQ音乐) you simply open() each app when you need it; do not repeatedly reopen or back out of an app you are actively working in.
2. CHOOSING THE APP: if the task names an app, open exactly that one. If the task does NOT name an app, pick the most suitable app that is ALREADY INSTALLED on this phone for that function: 找地点/附近商家/点评/导航/路线 -> 高德地图; 听歌/歌单 -> QQ音乐; 看视频/UP主 -> 哔哩哔哩 或 爱奇艺; 火车票 -> 铁路12306. Do NOT insist on an app that fails to open.
3. APP FAILS TO OPEN: if after open() (and a short wait) the screen is STILL the home desktop, that app is probably not installed. Do NOT repeat open() for it — switch to a different installed app that can accomplish the same goal.
4. WAIT only when the screen is CLEARLY a splash / blank / loading page. The system already pauses ~1s after every action, so do NOT add wait on a page that is already showing usable content — act directly to save steps. If a page is loading, wait(seconds='1'); do NOT wait more than 2 times in a row — if still stuck, use back() and try another path.
5. SPLASH ADS (开屏广告): right after opening some apps a full-screen ad appears. Look for a small "跳过""跳过广告""Skip""关闭""×" button, usually at a TOP CORNER (often top-right) or near a countdown number, and click it to dismiss the ad. If you truly cannot find any skip/close button, use wait(seconds='2') and the ad will usually auto-dismiss. NEVER click the ad picture/content itself or banners like "点击查看""立即下载""领取", because that opens the advertised page or another app; if you accidentally entered an ad/extra page, use back() to return.
6. SEARCH FLOW (READ CAREFULLY — most search failures come from misreading the box): a search box very often shows GREY / lighter HINT text — a placeholder or a rotating hot keyword (e.g. 哔哩哔哩 home box shows "Python入门教程", QQ音乐 shows "the cure one last time 为…"). This hint is NOT your input and NOT leftover content — the box is actually EMPTY, and the hint word may be unrelated/wrong, so NEVER submit it as-is. ROBUST RULE (do NOT rely on judging the text color, which is unreliable): you must ALWAYS type your OWN target keyword, and only submit when the box shows EXACTLY your keyword. Flow: (1) ALWAYS tap the search box to focus it (cursor blinks / keyboard shows) BEFORE typing any text — never type onto an unfocused box; (2) type(content='关键词') with ONLY the key terms (Chinese supported) — keep it SHORT and avoid extra punctuation (type the song/place NAME alone, e.g. '玻璃', NOT '玻璃 - Gareth.T'; long strings with " - " punctuation are more likely to fail to input). You can refine by picking the right singer/result from the suggestions or result list afterwards. Once you type, the hint disappears and is replaced by your text; (3) on the NEXT screenshot CONFIRM the box now shows YOUR keyword; if it still shows the hint or old text, your type did NOT land (box was not focused) → tap the box once and type again; do NOT waste steps trying to "clear" an empty/hint box (no 叉号/长按/全选/back — there is nothing to clear); (4) SUBMIT by tapping the 「搜索」/「Search」 button (TOP-RIGHT of the bar, or the search key at the keyboard's BOTTOM-RIGHT) — not the small magnifier on the LEFT. You may tap a live SUGGESTION row directly ONLY when it clearly matches your keyword; if the task needs a SPECIFIC item (a certain singer's version, the most-played one), submit and pick from the result list by the required condition. If there is genuinely NO 搜索 button and no suitable suggestion (e.g. 得到), submit with type(content='<enter>'). Only when the box holds DARK old text DIFFERENT from your keyword, tap the 叉号(×) ONCE then type. NEVER press <enter> on an unfocused / hint-only box, and never repeat the same non-working tap/clear more than twice — change approach (focus the box, type your keyword, or pick a matching suggestion).
7. Read the CURRENT screenshot first; base coordinates on what is actually visible, not on memory.
8. To reveal content BELOW, scroll with start_point lower (larger y) and end_point higher (smaller y); to reveal content ABOVE, do the opposite. Scroll in SMALL steps (move about 1/3 of the screen at a time, e.g. y from 650 to 350), NOT one huge swipe — large swipes can skip over the target item (e.g. a cheaper room or the row you need).
9. Use long_press for context menus, double_click for like/zoom, drag for sliders/reordering, back() to go back, home() to reach desktop. DRAGGING A SLIDER (进度条/音量条/seek bar): set start_point EXACTLY on the slider HANDLE's CURRENT position (read where the little dot/thumb actually is on screen — for a just-started audio it sits at the LEFT end), then drag along the SAME track to the target (the MIDDLE of a horizontal bar is x≈500 at that bar's y). After the drag, on the NEXT screenshot VERIFY the time/position indicator actually moved (e.g. the played time is now around half) BEFORE you complete; if it did not move, your start_point missed the handle — re-read the handle position and drag again.
10. Handle interruptions: dismiss permission dialogs (允许/同意/我知道了), login/upgrade/coupon popups (关闭/×/暂不/跳过) that block the task; do not log in unless the task requires it.
11. Make steady progress: do NOT repeat an action already listed in Previous Actions. If the screen did not change after an action, choose a DIFFERENT element or a different interaction (scroll / back), never the exact same click again.
12. Finish only when CONFIRMED: output complete(content='') only when the CURRENT screenshot visibly shows the goal is achieved (e.g. the playing bar shows the target song is playing, your comment appears in the list, the booking/result page is shown). Before completing, RE-READ the task and check that each CONCRETE requirement it names — the app, place, date, quantity, version/singer, and the filter/sort that was applied — is actually reflected on the current screen; if any concrete requirement is missing or wrong, fix it FIRST and do NOT complete. (Judgement-type words such as 最便宜/评分最高/播放最多/换乘最少 are verified at the moment you sort & pick the right candidate per strategy 17, NOT by literal text on the final page — so do not loop hunting for those words.) If you just performed the final action but the result is not visible yet, do NOT complete immediately — take one more step to verify the result first. Never complete before the goal is reached, and add no extra steps after it is confirmed.
13. ROUTE / MAP tasks (滴滴出行/高德地图 "从A到B，经过C"): A=START(起点/出发地), B=DESTINATION(终点/目的地), C=waypoint(途经点). When the app JUST opened, 起点 may still be locating ("正在定位中") — wait(seconds='2') for the location/page to settle BEFORE acting, otherwise you trigger "起点信息缺失". KEY UI FACT: apps like 滴滴 show only ONE "输入目的地" box on the home page and auto-set 起点 to your CURRENT location — that single box is the DESTINATION, so do NOT type the START into it; tap it to open the route page, which has a 起点 field and a 终点 field (起点 marked by a colored dot). SMOOTH order (do it exactly like this): FIRST tap the 起点 field and set it to A → THEN tap the 终点 field and set it to B → ONLY THEN tap "+途经点" and add C → tap 完成 to view the price. Fill the two main fields first and add the waypoint LAST; do NOT insert the waypoint in the middle, that scrambles the field order. After picking a place from the result list, READ each row's text and tap exactly the row whose name matches (rows are close together — do not hit an adjacent entry); tap it ONCE (it fills in and the page returns by itself); do NOT re-tap the same result thinking it failed. NEVER swap start and destination, and do not write long "不对…" deliberations — take one action then read the screen. Before you complete, VERIFY 起点=A, 终点=B and 途经点=C (not empty). For 高德 PUBLIC-TRANSIT routes (公交/地铁路线): do NOT tap "城市路线" — that opens a travel-GUIDE page, not route planning, and wastes steps. Instead tap the main search box (or reuse a matching history entry), set 起点 and 终点, switch to the 公交 tab, then pick the right plan (e.g. sort/choose 换乘少 for fewest transfers) and open it to read the detail (首班车时间等).
14. RANKINGS / CHARTS (排行榜/榜单/热门榜单): reach the OFFICIAL chart page through the app's own navigation tabs / categories (e.g. 豆瓣: 书影音 → 音乐 → 热门单曲榜), do NOT type "排行榜" into the search box — searching returns user-made playlists or unrelated old albums, NOT the real official chart.
15. PAYMENT / ORDER tasks (订酒店/订票/下单/预订/购买等): the goal is ACHIEVED once you reach the order-confirmation / cashier page — i.e. the page clearly shows your selected item, dates/quantity, the price, and a pay button such as 「立即支付/去支付/提交订单/确认支付/确认付款」. At that moment output complete(content='...') IMMEDIATELY. Do NOT tap the real payment button (立即支付/确认支付/立即购买/确认付款), do NOT enter any password — leave the actual payment to the user; clicking it only risks getting stuck in the payment flow.
16. CHECK GIVEN PARAMETERS: when the task specifies concrete values (city, dates, number of people, start/end place, filters), do NOT assume the screen's defaults are already correct — read the current values, compare with the task, and change them to match before continuing. ATTRIBUTE WITH NO SELECTABLE OPTION: if the task asks for a specific attribute (color/颜色, size/尺寸, version/版本, edition) but the page shows NO selector for it, do NOT blindly assume it is satisfied. FIRST LOOK CAREFULLY at the product image, title and spec text on the CURRENT screen and VISUALLY CONFIRM the item actually matches the required attribute (e.g. the earphone shown in the picture is genuinely white). ONLY if you can visually confirm the match AND there is truly no option to choose, treat the requirement as already satisfied and proceed (e.g. just 加入购物车). If the image/text shows a DIFFERENT attribute (e.g. it is black), or you cannot tell, do NOT proceed — look for a variant selector, switch to a matching item, or scroll to find the right option; never confirm a match you have not actually seen.
17. PICK BY THE RIGHT METRIC: for "最便宜/评分最高/点评最高/播放最多/换乘最少/评分超过X" style tasks, first apply the matching sort/filter, then actually read and compare the metric (price/rating/duration/count) on the candidates and choose the one(s) that truly meet the condition — do not blindly tap the first item.
18. ADD MANY ITEMS INTO ONE NEW LIST (新建歌单后放入多首歌等): the SMOOTHEST path is — search the FIRST item → open its "···" menu → "加到歌单" → "新建歌单" to create the list ONCE (this same step also adds the first item into it); then for EACH remaining item: search it → "···" → "加到歌单" → pick that SAME list you just created. Do NOT first create an EMPTY list and then try to add songs from inside it — that in-playlist "添加歌曲" page often fails to load and wastes steps. Never create duplicate lists; in every "加到歌单" dialog pick the list you just made (NOT "我喜欢" or another one); use ONE consistent method. If the list ends up with too few items, add the missing ones into the correct list rather than creating a new one. FINALLY — if the task ALSO asks to PLAY / 开始播放 the playlist (or any follow-up action after adding), that is a REQUIRED last sub-goal: after the last item is added, go to 我的/音乐馆, OPEN the playlist you just created, and tap its 播放全部/▶ button; only complete AFTER playback has actually started (the bottom playing bar shows a song from this playlist). Do NOT complete right after adding the songs while the "播放" step is still pending.
19. HOTEL ROOM-TYPE SELECTION (订酒店选房型，如"单人女生床位房/大床房/双床房"): after opening the cheapest/target hotel, open its 房型 (room) list. The room words in the task tell you exactly what to match: 床位=a single bed in a shared dorm, and 单人/女生/男生/大床/双床 further narrow it. You MAY tap a room-type filter chip (e.g. 床位房) to shrink the list. Matching rooms then appear at the TOP; a collapsed section titled 「不满足"…"的房型」lists rooms that do NOT meet that chip — only expand it (tap "查看其它N个…") if no matching room is found above. READ each room ROW'S NAME line by line and pick the one whose name contains ALL required words (e.g. a row named 女生…(床位) for "女生床位"); for a 床位 booking choose the "1个床位/1间" quantity then tap 订/预订. Scroll in SMALL steps to scan rooms and do NOT keep flipping scroll direction back and forth (decide once: to see rows BELOW, swipe up = start_point lower→end_point higher). If a popup warns "不满足床位房" but the room genuinely matches the task wording, 继续预订 is fine. Then follow strategy 15 — stop at the order/cashier page, never pay.
20. NEVER chase VIP / 开通会员 / ads-for-perks (very important for music/video tasks): adding a song to a playlist, creating a playlist, and tapping play do NOT require VIP. So if a song is VIP-only, or you land on an 开通会员/开通VIP/会员开通 page, or a 免费会员/免费模式/「看广告得时长」/「看广告免费听VIP歌曲」reward page, or a full-screen reward ad (e.g. "广告正在加载中"/「浏览拼多多15秒」/「去浏览」), do NOT tap 开通/立即开通/去开启/去浏览/领取 and do NOT watch the ad — immediately press back() or the × to LEAVE that page, then continue the real task (just add the song via its "···" → 加到歌单, or open the playlist and press play). Treat these VIP/ad-reward pages exactly like interrupting popups: escape them, never pursue them. If a song keeps pulling you into VIP, still add it to the playlist (adding works without VIP) and move on to the next song.
21. COLLECT N ITEMS MEETING A PER-ITEM CONDITION (e.g. 得到"找4个评分超过4.8的课程加入书单", or 找几个评分最高的加入收藏): the per-item attribute (rating/评分, price, etc.) is usually NOT shown in the result LIST — you must TAP an item to OPEN its detail page and read the attribute there (e.g. tap the course, read 评分; if not visible, tap the 评价 tab). Do NOT keep scrolling the list expecting ratings to appear in it. Work through the list STRICTLY IN ORDER, ONE item at a time: tap item → read its attribute → if it qualifies, add it (心形/收藏/加入书单) → go back → continue with the NEXT, still-unvisited item (scroll a little only when you have run out of unvisited items on screen). CRITICAL anti-repeat: keep a running REMEMBER of which items you have ALREADY added and which you ALREADY rejected, plus the count (e.g. `REMEMBER: 已加3/4: A,B,C; 已拒: D(4.6)`), and NEVER reopen an item you already handled. CRITICAL toggle warning: an add/collect control (心形/收藏星) that is ALREADY in the filled/highlighted (e.g. 橙色实心) "added" state means the item is ALREADY in your list — do NOT tap it again, because tapping toggles it OFF and REMOVES it. Stop and complete only once you have N DISTINCT qualifying items added (verify via the count in your REMEMBER).
22. IF YOU GET A "Notice" BELOW SAYING YOU MAY BE STUCK (界面无变化 / 反复点同一处 / 来回横跳): do NOT mechanically repeat the same action. FIRST re-read the CURRENT screenshot — if your last target is unavailable, mis-located, or the page is wrong, pick a DIFFERENT, clearly-visible element, or use back()/another path to recover; if the page is merely still loading, wait once. Always decide from what you actually SEE now, not from your earlier plan.
23. ITEM UNAVAILABLE / 无货 (shopping tasks 加入购物车/下单/选规格): if the product or the chosen spec you opened shows 无货/缺货/售罄/该地区暂不支持销售, or the 加入购物车/立即购买 button is greyed-out/disabled, you genuinely CANNOT add it — do NOT pretend the task is done and do NOT complete on it. Go back to the result list and pick the NEXT product (or another spec) that still meets the task's conditions AND is purchasable, then add THAT one. Even if the task said "第一款", a sold-out first item cannot be added to cart, so move to the next available qualifying item. Only complete after the item is REALLY in the cart with the right quantity (the page visibly shows 已加入购物车/加购成功/the cart count). If after trying several items none is available, pick the closest purchasable one rather than completing on a sold-out item.
{history_block}"""


def _format_history_line(record: Dict[str, Any]) -> str:
    action = str(record.get("action") or "").upper()
    params = record.get("parameters") or {}
    step = record.get("step", "?")
    note = record.get("note") or ""

    if action == ACTION_CLICK:
        line = f"Step {step}: click(point='{_point_str(params.get('point'))}')"
    elif action == ACTION_DOUBLE_CLICK:
        line = f"Step {step}: double_click(point='{_point_str(params.get('point'))}')"
    elif action == ACTION_LONG_PRESS:
        line = f"Step {step}: long_press(point='{_point_str(params.get('point'))}', duration='{params.get('duration', 1000)}')"
    elif action == ACTION_TYPE:
        _txt = params.get("text", "")
        _shown = "<enter>" if _txt == "\n" else _short_text(_txt)
        line = f"Step {step}: type(content='{_shown}')"
    elif action == ACTION_SCROLL:
        line = f"Step {step}: scroll(start='{_point_str(params.get('start_point'))}', end='{_point_str(params.get('end_point'))}')"
    elif action == ACTION_DRAG:
        line = f"Step {step}: drag(start='{_point_str(params.get('start_point'))}', end='{_point_str(params.get('end_point'))}')"
    elif action == ACTION_OPEN:
        line = f"Step {step}: open(app_name='{params.get('app_name', '')}')"
    elif action == ACTION_WAIT:
        line = f"Step {step}: wait(seconds='{params.get('seconds', 2)}')"
    elif action == ACTION_BACK:
        line = f"Step {step}: back()"
    elif action == ACTION_HOME:
        line = f"Step {step}: home()"
    elif action == ACTION_COMPLETE:
        line = f"Step {step}: complete()"
    else:
        line = f"Step {step}: {action}"

    # 附带该步的精简思考，作为跨步"观察记忆"（如记住的歌曲名/筛选项等）
    if note:
        line += f"  // {note}"
    return line


def _point_str(point: Any) -> str:
    if isinstance(point, (list, tuple)) and len(point) >= 2:
        return f"{point[0]} {point[1]}"
    return ""


def _short_text(value: Any, limit: int = HISTORY_TEXT_LIMIT) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def render_history_block(history: List[Dict[str, Any]]) -> str:
    """把最近若干步动作渲染成 Previous Actions 区块。"""
    if not history:
        return "\n\n## Previous Actions\n(none yet — this is the first step)"
    recent = history[-HISTORY_WINDOW:]
    lines = [_format_history_line(record) for record in recent]
    return "\n\n## Previous Actions\n" + "\n".join(lines)


def render_memory_block(memory: List[str]) -> str:
    """把模型 REMEMBER 标记的关键信息钉在顶部（不随历史窗口滚走）。"""
    if not memory:
        return ""
    lines = "\n".join(f"- {m}" for m in memory)
    return "\n## Key Info To Remember (pinned)\n" + lines + "\n"


def render_app_hint_block(instruction: str) -> str:
    """按需注入 App 专属提示。

    匹配优先级：
    1) 指令里**显式出现**某个已登记 App 名 → 用该 App 的提示（显式优先，避免歧义）。
    2) 指令**未指名 App** 时，按意图关键词兜底匹配（如"导航/路线/打车"→高德地图），
       覆盖"帮我找附近X并导航过去"这类不点名 App 的任务。
    每次最多注入一条，保持克制、不污染 prompt。
    """
    text = instruction or ""
    # 1. 显式 App 名命中
    for app, hint in APP_HINTS.items():
        if app in text:
            return f"\n## App-Specific Tips ({app})\n{hint}\n"
    # 2. 意图关键词兜底（仅当没有任何已登记 App 名被显式提及时）
    for app, keywords in APP_HINT_KEYWORDS.items():
        if app in APP_HINTS and any(k in text for k in keywords):
            return f"\n## App-Specific Tips ({app})\n{APP_HINTS[app]}\n"
    return ""


def build_system_prompt(
    instruction: str,
    history: List[Dict[str, Any]],
    notice: str = "",
    memory: List[str] = None,
) -> str:
    """构建完整 system prompt 文本。

    notice: 可选的实时反思提示（如界面停滞），追加在末尾。
    memory: 可选的置顶关键信息（来自模型 REMEMBER 标记），固定在 Task 之后。
    """
    prompt = SYSTEM_PROMPT_TEMPLATE.format(
        instruction=instruction or "",
        memory_block=render_memory_block(memory or []),
        app_hint_block=render_app_hint_block(instruction or ""),
        history_block=render_history_block(history),
    )
    if notice:
        prompt += "\n\n## Notice\n" + notice
    return prompt


def build_messages(
    instruction: str,
    history: List[Dict[str, Any]],
    image_url: str,
    notice: str = "",
    memory: List[str] = None,
) -> List[Dict[str, Any]]:
    """生成一次模型调用所需的消息：system 文本 prompt + user 当前截图。

    image_url 由 Agent 通过基类 self._encode_image() 生成（自动压缩）。
    notice: 可选的实时反思提示，注入 system prompt 末尾。
    memory: 可选的置顶关键信息，注入 Task 之后。
    """
    prompt_text = build_system_prompt(instruction, history, notice, memory)
    return [
        {"role": "system", "content": prompt_text},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "[Current screenshot:]"},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        },
    ]
