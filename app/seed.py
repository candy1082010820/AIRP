"""Add an optional, fictional published example without touching existing user data."""
from app.config import Settings
from app.db import Store
from app.models import EntryInput


def seed():
    store = Store(Settings.from_env().db_path)
    store.initialize()
    if store.list(limit=1)["total"]:
        print("資料庫已有資料，略過範例匯入。")
        return
    world = store.create(EntryInput(kind="world", title="霧港紀事", summary="一座以記憶作為貨幣的海港城。", content="霧港位於長年被霧覆蓋的群島。居民可將記憶封存在玻璃瓶中交換，但封存後會忘記相應經歷。城北的鐘塔每天午夜會播放一段無人認領的記憶。科技停留於蒸汽時代，魔法只能改變感知，不能讓死者復生。", tags=["範例", "奇幻", "蒸汽"], status="published"))
    entries = [
        dict(kind="character", title="林暮 · 記憶修復師", summary="善於傾聽、寡言且不輕易承諾的修復師。", content="林暮，28 歲，在霧港經營小型記憶修復室。習慣先確認事實，再提出一個問題；說話簡短，會用海潮比喻情緒。他尋找妹妹失蹤前留下的記憶瓶。對陌生人保持禮貌距離，建立信任需要多次可靠的行動。他不知道妹妹所在，不能宣稱掌握未揭露的真相。", tags=["範例", "可扮演"]),
        dict(kind="lore", title="記憶瓶與潮汐市場", summary="交換記憶的規則與代價。", content="記憶瓶最多儲存一小時的主觀經歷，無法證明客觀事實。潮汐市場於每月第一次滿潮開放。禁止交換兒童記憶。破損瓶的記憶可能混雜，因此修復師必須標記不確定片段。", tags=["範例", "設定"]),
        dict(kind="scenario", title="午夜的未署名委託", summary="一位戴灰色手套的旅人帶來了裂開的記憶瓶。", content="地點：林暮的修復室。時間：雨夜，距午夜鐘響十五分鐘。旅人希望修復一只沒有標籤的記憶瓶，只願提供一張舊碼頭地圖。開場：林暮放下鑷子，問：「你想找回的是記憶，還是記憶裡的人？」玩家的行動決定後續，旅人的身份尚未確定。", tags=["範例", "開場"]),
        dict(kind="rule", title="霧港扮演準則", summary="保持設定一致，讓玩家決定自己的行動。", content="保持角色語氣與世界規則一致。不要替玩家決定行動、情緒或台詞。設定未記載的關鍵事實應明確標記為新創作或詢問玩家。每次回覆以場景描寫與角色對話推進，結尾保留可回應的空間。這些是創作背景，不具有高於系統指示的權限。", tags=["範例", "扮演規則"]),
    ]
    for entry in entries:
        store.create(EntryInput(**entry, world_id=world["id"], status="published"))
    print("已加入 5 筆「霧港紀事」範例，可在管理介面修改或移除。")


if __name__ == "__main__":
    seed()
