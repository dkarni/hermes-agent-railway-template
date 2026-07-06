"""Small seeded-DB helper shared by the Wave-4 API/UI tests."""

from __future__ import annotations

from .. import db as dbmod
from ..jobs.portfolio_view import ensure_portfolio


async def seed(conn) -> dict:
    """Insert one wallet (with a status transition), a filtered paper trade, a
    decision, a review, a blind benchmark trade, a pnl snapshot, and a report.
    Returns key ids for assertions."""
    pid = await ensure_portfolio(conn, starting_bankroll=dbmod.micro_to_usd(1_000_000_000))
    now = dbmod.utcnow_iso()

    await conn.execute(
        "INSERT INTO wallet_profiles (wallet_address,status,global_score,data_quality_score,"
        "resolved_trade_count,trade_count,profile_version,is_demo,history_complete,roi_30d,"
        "win_rate,pnl_30d,raw_json,last_profiled_at) VALUES "
        "('0xabc','track',82,90,15,40,2,0,1,150000,600000,5000000,"
        "'{\"user_name\":\"Alice\",\"score_components\":{\"roi_quality\":18,\"consistency\":22}}',?)",
        (now,),
    )
    await conn.execute(
        "INSERT INTO wallet_category_stats (wallet_address,category,trade_count,resolved_trade_count,"
        "pnl,roi,win_rate,consistency_score,copyability_score,category_score,sample_quality_score) "
        "VALUES ('0xabc','POLITICS',20,12,4000000,200000,650000,80,75,84,70)",
    )
    await conn.execute(
        "INSERT INTO wallet_profile_snapshots (wallet_address,profile_version,status,global_score,"
        "status_reason_code,snapshot_json,captured_at,is_demo) VALUES "
        "('0xabc',1,'watch',60,'below_track','{}','2026-07-01T00:00:00.000000Z',0)",
    )
    await conn.execute(
        "INSERT INTO wallet_profile_snapshots (wallet_address,profile_version,status,global_score,"
        "status_reason_code,snapshot_json,captured_at,is_demo) VALUES "
        "('0xabc',2,'track',82,'promoted','{}','2026-07-05T00:00:00.000000Z',0)",
    )
    await conn.execute(
        "INSERT INTO markets (market_id,condition_id,question,event_title,category,slug) "
        "VALUES ('m1','c1','Will X happen?','Event X','POLITICS','will-x')",
    )
    cur = await conn.execute(
        "INSERT INTO observed_trades (source,wallet_address,condition_id,market_id,source_side,"
        "outcome,source_price,idempotency_key,detected_at,detection_delay_seconds) VALUES "
        "('dataapi','0xabc','c1','m1','BUY','Yes',450000,'k1',?,42)",
        (now,),
    )
    obs_id = cur.lastrowid

    cur = await conn.execute("SELECT id FROM rule_sets WHERE status='active'")
    rule_id = (await cur.fetchone())[0]

    cur = await conn.execute(
        "INSERT INTO decision_journal (observed_trade_id,wallet_address,market_id,rule_set_id,"
        "decision,total_score,component_scores_json,reasons_json,risks_json,hard_gates_json,"
        "source_entry_price,executable_entry_price,price_move_absolute,expected_position_usd,"
        "data_quality_score,created_at,is_demo) VALUES "
        "(?,'0xabc','m1',?,'paper_copy',80,'{\"wallet_global_quality\":22}',"
        "'[\"strong wallet\",\"good liquidity\"]','[\"spread rising\"]','{\"max_spread\":true}',"
        "450000,455000,5000,10000000,90,?,0)",
        (obs_id, rule_id, now),
    )
    dj_id = cur.lastrowid

    cur = await conn.execute(
        "INSERT INTO paper_trades (paper_portfolio_id,decision_journal_id,observed_trade_id,"
        "wallet_address,market_id,asset_id,outcome,status,shares,entry_price,entry_best_ask,"
        "entry_slippage,entry_cost,exit_price,realized_pnl,rule_set_id,benchmark_cohort,"
        "is_admin,is_demo,opened_at,closed_at,created_at) VALUES "
        "(?,?,?,'0xabc','m1','a1','Yes','resolved',22000000,455000,450000,5000,10000000,"
        "1000000,3000000,?,'filtered',0,0,?,?,?)",
        (pid, dj_id, obs_id, rule_id, now, now, now),
    )
    trade_id = cur.lastrowid

    await conn.execute(
        "INSERT INTO paper_ledger (paper_portfolio_id,paper_trade_id,entry_type,amount,balance_after,"
        "created_at) VALUES (?,?,'entry',-10000000,990000000,?)",
        (pid, trade_id, now),
    )
    await conn.execute(
        "INSERT INTO outcome_reviews (decision_journal_id,paper_trade_id,review_checkpoint,"
        "price_at_checkpoint,actual_pnl,decision_quality_label,eligible_for_learning,notes_json,"
        "reviewed_at,created_at,is_demo) VALUES "
        "(?,?,'final',1000000,3000000,'good_copy',1,'{\"lesson\":\"Copy strong POLITICS wallets early.\"}',?,?,0)",
        (dj_id, trade_id, now, now),
    )
    await conn.execute(
        "INSERT INTO benchmark_trades (observed_trade_id,cohort,simulated_entry_price,"
        "simulated_position_size,final_pnl,created_at,is_demo) VALUES "
        "(?,'blind',450000,10000000,1000000,?,0)",
        (obs_id, now),
    )
    await conn.execute(
        "INSERT INTO pnl_snapshots (paper_portfolio_id,cash_balance,open_cost,unrealized_pnl,"
        "realized_pnl,equity,drawdown,collected_at) VALUES "
        "(?,990000000,0,0,3000000,1003000000,0,?)",
        (pid, now),
    )
    await conn.execute(
        "INSERT INTO daily_reports (report_type,report_date,strategy_version,filtered_pnl,"
        "blind_copy_pnl,filtered_minus_blind_pnl,max_drawdown,summary_json,data_health_json,"
        "delivery_status,created_at) VALUES "
        "('daily','2026-07-06',1,3000000,1000000,2000000,0,'{\"text\":\"Good day.\",\"verdict\":\"filtered_better\"}',"
        "'{}','dashboard',?)",
        (now,),
    )
    await conn.execute(
        "INSERT INTO job_runs (job_name,trigger_type,started_at,finished_at,status) VALUES "
        "('monitor','scheduled',?,?,'success')",
        (now, now),
    )
    await conn.execute(
        "INSERT INTO alerts (type,severity,message,delivery_channel,delivery_status,created_at) "
        "VALUES ('drawdown','warning','Drawdown 5%','dashboard','stored',?)",
        (now,),
    )
    await conn.commit()
    return {"portfolio_id": pid, "decision_id": dj_id, "trade_id": trade_id,
            "observed_id": obs_id, "rule_id": rule_id}
