with open("src/trading/strategy_engine.py", "r", encoding="utf-8") as f:
    code = f.read()

	old_oracle_block = """        # ─── MACRO REGIME CIRCUIT BREAKER (ACTIVE FROM 09:15 OPENING BELL) ───
	        macro_regime = None
			        try:
					            macro_regime = oracle.get_regime(tick_epoch_ms)
								            eval_meta = getattr(oracle, 'last_eval', {})
											            autopsy_entry["ml_regime"] = eval_meta.get("regime_name", str(macro_regime))
														            autopsy_entry["prob_bull"] = round(eval_meta.get("prob_bull", 0.0) * 100, 1)
																	            autopsy_entry["prob_bear"] = round(eval_meta.get("prob_bear", 0.0) * 100, 1)
																				            autopsy_entry["prob_chop"] = round(eval_meta.get("prob_chop", 0.0) * 100, 1)
																							            autopsy_entry["ml_edge_pct"] = round(eval_meta.get("edge", 0.0) * 100, 1)
																										        except Exception as e:
																												            print(f"[ORACLE EXCEPTION] ML Fault: {e}. Falling back to Pure Math.")
																															            macro_regime = None
																																		            autopsy_entry["ml_regime"] = "EXCEPTION_BYPASS" """

																																					new_stock_brain_block = """        # ─── PER-STOCK MICRO-BOT BRAIN (REPLACES GLOBAL NIFTY VETO) ───
																																					        macro_regime = None
																																							        try:
																																									            bot_eval = bot.last_ml_eval
																																												            ai_bias = bot.profile.get("direction_bias", "NEUTRAL")
																																															            ai_score = float(bot.profile.get("ai_score", 0.50))

																																																		            if bot_eval.get("regime") is not None:
																																																					                macro_regime = bot_eval["regime"]
																																																									                reg_label = "BOT_BULL" if macro_regime == 1 else ("BOT_BEAR" if macro_regime == -1 else "BOT_CHOP")
																																																													                autopsy_entry["ml_regime"] = reg_label
																																																																	                autopsy_entry["prob_bull"] = round(bot_eval.get("prob_bull", 0.0) * 100, 1)
																																																																					                autopsy_entry["prob_bear"] = round(bot_eval.get("prob_bear", 0.0) * 100, 1)
																																																																									                autopsy_entry["prob_chop"] = round(bot_eval.get("prob_chop", 0.0) * 100, 1)
																																																																													                autopsy_entry["ml_edge_pct"] = round(bot_eval.get("edge", 0.0) * 100, 1)
																																																																																	            elif ai_bias in ("LONG", "BULLISH") or ai_score >= 0.60:
																																																																																				                macro_regime = 1
																																																																																								                autopsy_entry["ml_regime"] = "AI_CATALYST_BULL"
																																																																																												                autopsy_entry["prob_bull"] = round(ai_score * 100, 1)
																																																																																																            elif ai_bias in ("SHORT", "BEARISH") or ai_score <= 0.40:
																																																																																																			                macro_regime = -1
																																																																																																							                autopsy_entry["ml_regime"] = "AI_CATALYST_BEAR"
																																																																																																											                autopsy_entry["prob_bear"] = round((1.0 - ai_score) * 100, 1)
																																																																																																															            else:
																																																																																																																		                macro_regime = None
																																																																																																																						                autopsy_entry["ml_regime"] = "PURE_STOCK_MATH"
																																																																																																																										        except Exception as e:
																																																																																																																												            print(f"[BOT BRAIN EXCEPTION] {e}. Falling back to Pure Stock Math.")
																																																																																																																															            macro_regime = None
																																																																																																																																		            autopsy_entry["ml_regime"] = "EXCEPTION_BYPASS" """

																																																																																																																																					if old_oracle_block.strip() in code:
																																																																																																																																					    code = code.replace(old_oracle_block.strip(), new_stock_brain_block.strip(), 1)
																																																																																																																																						    with open("src/trading/strategy_engine.py", "w", encoding="utf-8") as f:
																																																																																																																																							        f.write(code)
																																																																																																																																									    print("[SUCCESS] Disconnected Nifty Veto! Each StockMicroBot now controls its own trades.")
																																																																																																																																										else:
																																																																																																																																										    print("[CHECK] Block already updated or modified.")
																																																																																																																																											EOF

																																																																																																																																											python3 switch_to_stock_brain.py

																																																																																																																																											# Restart only the Strategy Engine pane inside tmux (leaves WebSocket running)
																																																																																																																																											pkill -f "python3 -m src.trading.strategy_engine" || true
																																																																																																																																											tmux send-keys -t hft_live.3 "cd /home/ubuntu/project_pipeline && source venv/bin/activate && python3 -m src.trading.strategy_engine" C-m
																																																																																																																																											REMOTE_EOF
