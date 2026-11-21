"""读写一致性自测：范围查询使用的读事务在同一 WAL 快照上完成多条 SELECT。"""
import sqlite3

from app import db


def test_readonly_transaction_holds_snapshot_across_writes(tmp_path):
    path = str(tmp_path / "snap.db")
    db.init_db(path)

    conn = db.connect(path)
    conn.execute(
        "INSERT INTO trips (trip_code, departure_at, departure_date, active_in_new,"
        " created_at, updated_at) VALUES ('T1','2026-11-20T10:00:00Z','2026-11-20',1,'x','x')"
    )
    trip_id = conn.execute("SELECT id FROM trips WHERE trip_code='T1'").fetchone()["id"]
    conn.execute(
        "INSERT INTO batches (id, trip_id, version, submitted_at, point_count, points_hash)"
        " VALUES (1,?,1,'x',1,'h')",
        (trip_id,),
    )
    conn.execute(
        "INSERT INTO points (batch_id, seq, sampled_at, lat, lon)"
        " VALUES (1,1,'2026-11-20T10:00:00Z',30.0,120.0)"
    )
    conn.execute("UPDATE trips SET current_batch_id=1 WHERE id=?", (trip_id,))
    conn.commit()
    conn.close()

    with db.readonly(path) as reader:
        batch_before = reader.execute(
            "SELECT current_batch_id FROM trips WHERE id=?", (trip_id,)
        ).fetchone()["current_batch_id"]
        # 读事务进行中，另一个连接提交新批次并切换 current 指针
        writer = db.connect(path)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO batches (id, trip_id, version, submitted_at, point_count, points_hash)"
            " VALUES (2,?,2,'y',1,'h2')",
            (trip_id,),
        )
        writer.execute(
            "INSERT INTO points (batch_id, seq, sampled_at, lat, lon)"
            " VALUES (2,1,'2026-11-20T10:05:00Z',31.0,121.0)"
        )
        writer.execute("UPDATE trips SET current_batch_id=2 WHERE id=?", (trip_id,))
        writer.commit()
        writer.close()

        # 同一读事务内第二次读取仍是旧快照：不会一半改挂前、一半改挂后
        row = reader.execute(
            "SELECT current_batch_id FROM trips WHERE id=?", (trip_id,)
        ).fetchone()
        assert row["current_batch_id"] == batch_before == 1
        point = reader.execute(
            "SELECT seq, lat FROM points WHERE batch_id=1"
        ).fetchone()
        assert (point["seq"], point["lat"]) == (1, 30.0)

    # 新事务可见已提交的新批次
    with db.readonly(path) as reader:
        assert reader.execute(
            "SELECT current_batch_id FROM trips WHERE id=?", (trip_id,)
        ).fetchone()["current_batch_id"] == 2
