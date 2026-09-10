//+------------------------------------------------------------------+
//| POI_Retest_Engulf_EA.mq5                                         |
//|                                                                  |
//| MQL5 port of trading_bot's M15-POI / M5-engulfing strategy.      |
//| See SPECIFICATION.md in the Python project for the full spec.    |
//| This EA re-implements the same order-block detection, departure/ |
//| retest/engulf state machine and risk sizing, natively in MQL5,   |
//| so it can run as a real Expert Advisor attached to a chart       |
//| instead of the external run_live.py process.                     |
//|                                                                  |
//| Fidelity notes / deliberate simplifications vs the Python engine:|
//|  * poi_type is fixed to "order_block" (the SPECIFICATION.md's    |
//|    SELECTED definition; fvg/swing variants are not ported).      |
//|  * require_structure_break is not implemented (default: off).    |
//|  * Session filter supports exactly one daily window + Mon-Fri,   |
//|    matching the shipped default.yaml/live.yaml (a generic list   |
//|    of windows is not ported).                                    |
//|  * Entry uses the current market Ask/Bid the instant the signal  |
//|    confirms - the live equivalent of the Python "next_open" mode |
//|    (there is no discrete "next bar" to wait for in real time).   |
//|  * consecutive-loss tracking is done here via HistoryDeals scan; |
//|    the Python LIVE path (execution.py) never actually calls      |
//|    RiskManager.on_close(), so that brake is dead code there. This|
//|    port implements it for real, matching the documented intent.  |
//|  * Every other numeric threshold below mirrors trading_bot's     |
//|    Config dataclasses field-for-field (see config.py).           |
//+------------------------------------------------------------------+
#property copyright "Research code - no validated edge. See VALIDATION.md."
#property version   "1.00"
#property strict

//====================================================================
// Inputs (mirrors configs/live.yaml)
//====================================================================
input group "=== Safety switches (two independent gates, like run_live.py) ==="
input bool   InpIUnderstandTheRisk   = false;   // must be true, or no order is ever sent
input bool   InpAllowRealMoneyAccount= false;   // must ALSO be true if the account is REAL, not demo

input group "=== POI (order block) ==="
input int    InpAtrPeriod            = 14;
input bool   InpUseBodyOnly          = false;
input double InpZonePaddingPips      = 0.0;
input double InpMaxZoneWidthPips     = 18.0;
input double InpMinZoneWidthPips     = 0.5;
input int    InpExpiryM15Bars        = 96;
input int    InpMaxActivePois        = 6;
input double InpOverlapMinSeparationPips = 0.0;
input bool   InpPreferStrongestOnOverlap = false;  // false = "newest" (default), true = "strongest"
input double InpDisplacementAtrMult  = 1.0;
input int    InpMaxLegBars           = 5;
input int    InpMinLegBars           = 1;

input group "=== Retest ==="
input double InpMinDeparturePips     = 8.0;
input double InpMinDepartureAtr      = 0.0;
input int    InpMinM5BarsOutside     = 3;
input double InpMaxDeparturePips     = 250.0;
input int    InpMinM5BarsBeforeRetest= 0;
input int    InpMaxM5BarsToRetest    = 288;
input double InpTouchTolerancePips   = 0.5;
input double InpMaxPenetrationPct    = 1.0;
input bool   InpInvalidateOnCloseBeyondDistal = true;
input double InpInvalidationBufferPips = 1.0;
input bool   InpAllowWickOnlyTouch   = true;
input int    InpMaxRetests           = 1;

input group "=== Engulfing ==="
input int    InpMaxM5BarsAfterRetest = 6;
input bool   InpRequireBodyEngulf    = true;
input bool   InpEngulfUseRange       = false;
input double InpMinBodyRatio         = 1.0;
input double InpMinRangeRatio        = 0.0;
input double InpClosePositionPct     = 0.0;
input double InpMinBodyPips          = 0.5;
input double InpMaxBodyPips          = 40.0;
input bool   InpRequirePrevOpposite  = true;
input bool   InpRequireTouchZone     = true;
input double InpZoneProximityPips    = 2.0;

input group "=== Trade / SL / TP ==="
input double InpRR                   = 2.0;
input double InpSlBufferPips         = 1.0;
input double InpMaxSlPips            = 20.0;
input double InpMinSlPips            = 3.0;

input group "=== Session (UTC, Mon-Fri only) ==="
input int    InpSessionStartHourUTC  = 7;
input int    InpSessionStartMinUTC   = 0;
input int    InpSessionEndHourUTC    = 16;
input int    InpSessionEndMinUTC     = 0;
input double InpBrokerUtcOffsetHours = 0.0;   // broker/server time = UTC + this offset

input group "=== Risk ==="
input double InpRiskPerTrade         = 0.10;   // 10% - matches configs/live.yaml as of this session
input int    InpMaxOpenPositions     = 1;
input int    InpMaxTradesPerDay      = 4;
input double InpMaxDailyLossPct      = 0.35;
input int    InpMaxConsecutiveLosses = 4;
input int    InpCooldownM5BarsAfterLoss = 3;
input int    InpCooldownM5BarsAfterAny  = 0;
input double InpMaxSpreadPips        = 2.0;
input double InpCommissionPerLotRoundTurn = 7.0;
input bool   InpRejectIfBelowMinLot  = true;

input group "=== Mechanics ==="
input long   InpMagic                = 900101;
input int    InpHistoryM5Bars        = 3000;
input int    InpDeviationPoints      = 20;
input int    InpPollSeconds          = 15;

//====================================================================
// POI lifecycle states
//====================================================================
#define ST_CREATED  0
#define ST_DEPARTED 1
#define ST_RETESTED 2
#define ST_TRIGGERED 3
#define ST_INVALID  4
#define ST_EXPIRED  5
#define DIR_LONG  0
#define DIR_SHORT 1

bool IsActiveState(int st) { return st==ST_CREATED || st==ST_DEPARTED || st==ST_RETESTED; }

struct SPOI
{
   string   id;
   int      dir;
   double   upper;
   double   lower;
   datetime origin_time;
   datetime created_time;
   int      created_m5_index;
   double   strength;
   int      state;
   int      bars_outside;
   double   max_departure_pips;
   datetime departed_time;
   int      departed_index;
   datetime retest_time;
   int      retest_index;
   int      retest_count;
};

struct SCandidate
{
   int      dir;
   double   lo, hi;
   datetime origin_time, created_time;
   double   strength;
};

struct SSignal
{
   bool     valid;
   int      dir;
   double   poiUpper, poiLower;
   string   poiId;
   datetime engulfTime;   // OPEN time of the engulfing bar
   datetime signalTime;   // CLOSE time of the engulfing bar
};

//====================================================================
// Globals
//====================================================================
double   PipSize;
datetime g_lastProcessedBarTime = 0;
string   g_lastDay = "";
double   g_dayStartEquity = 0;
int      g_tradesToday = 0;
int      g_consecutiveLosses = 0;
bool     g_haltedToday = false;
datetime g_cooldownUntilTime = 0;
ulong    g_lastProcessedDealTicket = 0;
bool     g_warnedRisk = false;

//====================================================================
// OnInit / OnDeinit
//====================================================================
int OnInit()
{
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   PipSize = (digits==3 || digits==5) ? point*10.0 : point;

   g_dayStartEquity = AccountInfoDouble(ACCOUNT_EQUITY);
   g_lastProcessedDealTicket = 0;
   HistorySelect(0, TimeCurrent());   // warm the history cache once

   PrintFormat("POI_Retest_Engulf_EA init: symbol=%s pip=%.5f magic=%d risk_per_trade=%.2f%%",
               _Symbol, PipSize, (int)InpMagic, InpRiskPerTrade*100.0);
   if(!InpIUnderstandTheRisk)
      Print("SAFETY: InpIUnderstandTheRisk is false - the EA will monitor but NEVER send an order until you set it to true.");

   EventSetTimer(MathMax(InpPollSeconds,1));
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   Comment("");
}

void OnTick() { CheckAndProcess(); }
void OnTimer() { CheckAndProcess(); }

//====================================================================
// New-bar gate (mirrors LiveTrader.poll_once's "no new closed bar" check)
//====================================================================
void CheckAndProcess()
{
   MqlRates lastClosed[];
   if(CopyRates(_Symbol, PERIOD_M5, 1, 1, lastClosed) < 1) return;
   datetime barTime = lastClosed[0].time;
   if(g_lastProcessedBarTime!=0 && barTime<=g_lastProcessedBarTime) { UpdateDashboard(); return; }
   g_lastProcessedBarTime = barTime;
   Process();
   UpdateClosedTradeStats();
   UpdateDashboard();
}

//====================================================================
// M15 resampling from M5 (label=left, bar_end=label+15m), only closed buckets
//====================================================================
void BuildM15(const MqlRates &r[], int n,
              datetime &m15Start[], datetime &m15End[],
              double &o[], double &h[], double &l[], double &c[],
              int &readyAtM5[])
{
   ArrayResize(o,0); ArrayResize(h,0); ArrayResize(l,0); ArrayResize(c,0);
   ArrayResize(m15Start,0); ArrayResize(m15End,0); ArrayResize(readyAtM5,0);

   bool haveCur=false;
   long curStartSecs=0;
   double curO=0,curH=0,curL=0,curC=0;

   for(int i=0;i<n;i++)
   {
      long secs=(long)r[i].time;
      long bStart = secs - (secs % 900);
      if(!haveCur || bStart!=curStartSecs)
      {
         if(haveCur)
         {
            int idx=ArraySize(o);
            ArrayResize(o,idx+1); ArrayResize(h,idx+1); ArrayResize(l,idx+1); ArrayResize(c,idx+1);
            ArrayResize(m15Start,idx+1); ArrayResize(m15End,idx+1); ArrayResize(readyAtM5,idx+1);
            o[idx]=curO; h[idx]=curH; l[idx]=curL; c[idx]=curC;
            m15Start[idx]=(datetime)curStartSecs; m15End[idx]=(datetime)(curStartSecs+900);
            readyAtM5[idx]=i-1;      // last M5 bar of the bucket just closed
         }
         haveCur=true; curStartSecs=bStart;
         curO=r[i].open; curH=r[i].high; curL=r[i].low; curC=r[i].close;
      }
      else
      {
         curH=MathMax(curH,r[i].high);
         curL=MathMin(curL,r[i].low);
         curC=r[i].close;
      }
   }
   // deliberately do NOT finalize the last (still-forming) bucket
}

//====================================================================
// Wilder ATR on the M15 series (matches pandas ewm(alpha=1/period, adjust=False))
//====================================================================
void ComputeATR(const double &h[], const double &l[], const double &c[], int n, int period,
                 double &atrArr[], bool &validArr[])
{
   ArrayResize(atrArr,n); ArrayResize(validArr,n);
   double prevATR=0;
   for(int i=0;i<n;i++)
   {
      double tr;
      if(i==0) tr = h[0]-l[0];
      else
      {
         double a=h[i]-l[i];
         double b=MathAbs(h[i]-c[i-1]);
         double d=MathAbs(l[i]-c[i-1]);
         tr=MathMax(a,MathMax(b,d));
      }
      double atrVal = (i==0) ? tr : prevATR + (tr-prevATR)/period;
      atrArr[i]=atrVal;
      validArr[i]=(i>=period-1);
      prevATR=atrVal;
   }
}

//====================================================================
// Order-block POI detection at closed M15 bar j (mirrors POIDetector._order_blocks)
//====================================================================
void DetectOrderBlocks(int j, const double &o[], const double &h[], const double &l[], const double &c[],
                        const double &atr[], const bool &atrValid[],
                        const datetime &m15Start[], const datetime &m15End[],
                        SCandidate &out[])
{
   ArrayResize(out,0);
   int minJ = MathMax(InpAtrPeriod, InpMaxLegBars+1);
   if(j<minJ) return;

   for(int pass=0; pass<2; pass++)
   {
      bool bullish=(pass==0);
      if(bullish  && !(c[j]>o[j])) continue;
      if(!bullish && !(c[j]<o[j])) continue;

      int oIdx=-1;
      for(int k=1;k<=InpMaxLegBars;k++)
      {
         int idx=j-k;
         if(idx<1) break;
         if(bullish  && c[idx]<o[idx]) { oIdx=idx; break; }
         if(!bullish && c[idx]>o[idx]) { oIdx=idx; break; }
      }
      if(oIdx<0) continue;

      int leg=j-oIdx;
      if(leg<InpMinLegBars || leg>InpMaxLegBars) continue;
      if(!atrValid[oIdx]) continue;
      double a=atr[oIdx];
      if(a<=0) continue;

      double impulse;
      if(bullish)
      {
         if(!(c[j]>h[oIdx])) continue;
         impulse=c[j]-l[oIdx];
      }
      else
      {
         if(!(c[j]<l[oIdx])) continue;
         impulse=h[oIdx]-c[j];
      }
      if(impulse < InpDisplacementAtrMult*a) continue;

      double lo,hi;
      if(InpUseBodyOnly) { lo=MathMin(o[oIdx],c[oIdx]); hi=MathMax(o[oIdx],c[oIdx]); }
      else               { lo=l[oIdx];              hi=h[oIdx]; }

      double pad=InpZonePaddingPips*PipSize;
      double loP=lo-pad, hiP=hi+pad;
      double widthPips=(hiP-loP)/PipSize;
      if(widthPips<InpMinZoneWidthPips || widthPips>InpMaxZoneWidthPips) continue;

      int idx2=ArraySize(out);
      ArrayResize(out,idx2+1);
      out[idx2].dir = bullish?DIR_LONG:DIR_SHORT;
      out[idx2].lo=loP; out[idx2].hi=hiP;
      out[idx2].origin_time=m15Start[oIdx];
      out[idx2].created_time=m15End[j];
      out[idx2].strength=impulse/a;
   }
}

//====================================================================
// POI admission: overlap collapse + max_active_pois eviction (mirrors _admit)
//====================================================================
void AdmitPOI(SPOI &pois[], const SPOI &cand)
{
   int n=ArraySize(pois);
   int sameDirIdx[]; ArrayResize(sameDirIdx,0);
   for(int k=0;k<n;k++)
   {
      if(IsActiveState(pois[k].state) && pois[k].dir==cand.dir)
      {
         int sz=ArraySize(sameDirIdx); ArrayResize(sameDirIdx,sz+1); sameDirIdx[sz]=k;
      }
   }
   double sep=InpOverlapMinSeparationPips*PipSize;
   for(int idx=0; idx<ArraySize(sameDirIdx); idx++)
   {
      int k=sameDirIdx[idx];
      double overlap = MathMin(cand.upper,pois[k].upper) - MathMax(cand.lower,pois[k].lower);
      if(overlap > -sep)
      {
         if(InpPreferStrongestOnOverlap && pois[k].strength>=cand.strength)
            return;                       // new candidate rejected outright
         pois[k].state=ST_INVALID;
      }
   }
   if(ArraySize(sameDirIdx) >= InpMaxActivePois)
   {
      int oldestK=-1; datetime oldestT=0;
      for(int idx=0; idx<ArraySize(sameDirIdx); idx++)
      {
         int k=sameDirIdx[idx];
         if(oldestK==-1 || pois[k].created_time<oldestT) { oldestK=k; oldestT=pois[k].created_time; }
      }
      if(oldestK!=-1) pois[oldestK].state=ST_EXPIRED;
   }
   int sz=ArraySize(pois);
   ArrayResize(pois,sz+1);
   pois[sz]=cand;
}

void CompactActive(SPOI &pois[])
{
   int n=ArraySize(pois);
   SPOI tmp[]; ArrayResize(tmp,0);
   for(int k=0;k<n;k++)
   {
      if(IsActiveState(pois[k].state))
      {
         int sz=ArraySize(tmp); ArrayResize(tmp,sz+1); tmp[sz]=pois[k];
      }
   }
   ArrayResize(pois,ArraySize(tmp));
   for(int k=0;k<ArraySize(tmp);k++) pois[k]=tmp[k];
}

//====================================================================
// Engulfing test (mirrors indicators.is_engulfing)
//====================================================================
bool IsEngulfing(double prevOpen,double prevHigh,double prevLow,double prevClose,
                  double curOpen,double curHigh,double curLow,double curClose,
                  bool bullish)
{
   double EPS=1e-10;
   bool prevBearish = prevClose<prevOpen;
   bool prevBullish = prevClose>prevOpen;
   if(InpRequirePrevOpposite)
   {
      if(bullish  && !prevBearish) return false;
      if(!bullish && !prevBullish) return false;
   }
   bool curBullish=curClose>curOpen, curBearish=curClose<curOpen;
   if(bullish  && !curBullish) return false;
   if(!bullish && !curBearish) return false;

   double curTop=MathMax(curOpen,curClose), curBot=MathMin(curOpen,curClose);
   double prevTop=MathMax(prevOpen,prevClose), prevBot=MathMin(prevOpen,prevClose);
   if(InpRequireBodyEngulf)
      if(curBot>prevBot+EPS || curTop<prevTop-EPS) return false;
   if(InpEngulfUseRange)
      if(curHigh<prevHigh-EPS || curLow>prevLow+EPS) return false;

   double curBody=MathAbs(curClose-curOpen), prevBody=MathAbs(prevClose-prevOpen);
   double curRange=curHigh-curLow, prevRange=prevHigh-prevLow;
   if(InpMinBodyRatio>0  && curBody<InpMinBodyRatio*prevBody-EPS) return false;
   if(InpMinRangeRatio>0 && curRange<InpMinRangeRatio*prevRange-EPS) return false;
   if(InpMinBodyPips>0   && curBody<InpMinBodyPips*PipSize-EPS) return false;
   if(InpMaxBodyPips>0   && curBody>InpMaxBodyPips*PipSize+EPS) return false;
   if(InpClosePositionPct>0 && curRange>EPS)
   {
      double pos=(curClose-curLow)/curRange;
      if(bullish  && pos<1.0-InpClosePositionPct) return false;
      if(!bullish && pos>InpClosePositionPct) return false;
   }
   return true;
}

bool CheckEngulf(SPOI &p, datetime ts, datetime curBarOpenTime,
                  double prevOpen,double prevHigh,double prevLow,double prevClose,
                  double curOpen,double curHigh,double curLow,double curClose,
                  SSignal &sig)
{
   bool longDir=(p.dir==DIR_LONG);
   if(!IsEngulfing(prevOpen,prevHigh,prevLow,prevClose,curOpen,curHigh,curLow,curClose,longDir))
      return false;
   if(InpRequireTouchZone)
   {
      double prox=InpZoneProximityPips*PipSize;
      bool touch = longDir ? (curLow<=p.upper+prox) : (curHigh>=p.lower-prox);
      if(!touch) return false;
   }
   p.state=ST_TRIGGERED;
   sig.valid=true; sig.dir=p.dir; sig.poiUpper=p.upper; sig.poiLower=p.lower; sig.poiId=p.id;
   sig.engulfTime=curBarOpenTime; sig.signalTime=ts;
   return true;
}

//====================================================================
// Per-POI state machine (mirrors StrategyEngine._update_poi)
//====================================================================
bool UpdatePOI(SPOI &p, int i, datetime ts,
               double hi, double lo, double cl,
               double pOpen,double pHigh,double pLow,double pClose,
               double cOpen,double cHigh,double cLow,double cClose,
               datetime curBarOpenTime,
               double depAtr, bool depAtrValid,
               SSignal &sig)
{
   bool longDir=(p.dir==DIR_LONG);
   double tol=InpTouchTolerancePips*PipSize;
   double invalBuf=InpInvalidationBufferPips*PipSize;

   int ageBars=i-p.created_m5_index;
   if(ageBars > InpExpiryM15Bars*3) { p.state=ST_EXPIRED; return false; }

   if(InpInvalidateOnCloseBeyondDistal)
   {
      if(longDir  && cl<p.lower-invalBuf) { p.state=ST_INVALID; return false; }
      if(!longDir && cl>p.upper+invalBuf) { p.state=ST_INVALID; return false; }
   }

   if(p.state==ST_CREATED)
   {
      bool outside = longDir ? (lo>p.upper) : (hi<p.lower);
      p.bars_outside = outside ? p.bars_outside+1 : 0;
      double dist = longDir ? (hi-p.upper) : (p.lower-lo);
      double distPips=dist/PipSize;
      if(distPips>p.max_departure_pips) p.max_departure_pips=distPips;
      bool atrOk=true;
      if(InpMinDepartureAtr>0)
         atrOk = depAtrValid && (dist >= InpMinDepartureAtr*depAtr);
      if(p.bars_outside>=InpMinM5BarsOutside && p.max_departure_pips>=InpMinDeparturePips && atrOk)
      {
         p.state=ST_DEPARTED; p.departed_time=ts; p.departed_index=i;
      }
      return false;
   }

   if(p.state==ST_DEPARTED)
   {
      double dist = longDir ? (hi-p.upper) : (p.lower-lo);
      double distPips=dist/PipSize;
      if(distPips>p.max_departure_pips) p.max_departure_pips=distPips;
      if(p.max_departure_pips>InpMaxDeparturePips) { p.state=ST_EXPIRED; return false; }
      if(i-p.departed_index>InpMaxM5BarsToRetest)  { p.state=ST_EXPIRED; return false; }
      if(i-p.departed_index<InpMinM5BarsBeforeRetest) return false;

      bool touched = longDir ? (lo<=p.upper+tol) : (hi>=p.lower-tol);
      if(!touched) return false;
      if(!InpAllowWickOnlyTouch)
      {
         bool closedIn = longDir ? (cl<=p.upper+tol) : (cl>=p.lower-tol);
         if(!closedIn) return false;
      }
      double height=MathMax(p.upper-p.lower,1e-12);
      double pen = (longDir ? (p.upper-lo) : (hi-p.lower)) / height;
      if(pen>InpMaxPenetrationPct) { p.state=ST_INVALID; return false; }

      p.state=ST_RETESTED; p.retest_time=ts; p.retest_index=i; p.retest_count++;
      return CheckEngulf(p, ts, curBarOpenTime, pOpen,pHigh,pLow,pClose,cOpen,cHigh,cLow,cClose, sig);
   }

   if(p.state==ST_RETESTED)
   {
      if(i-p.retest_index>InpMaxM5BarsAfterRetest)
      {
         if(p.retest_count<InpMaxRetests) { p.state=ST_DEPARTED; return false; }
         p.state=ST_EXPIRED; return false;
      }
      return CheckEngulf(p, ts, curBarOpenTime, pOpen,pHigh,pLow,pClose,cOpen,cHigh,cLow,cClose, sig);
   }
   return false;
}

//====================================================================
// Full-history replay - rebuilt from scratch every call, exactly like
// LiveTrader.poll_once in the Python engine (see execution.py: "Why the
// engine replays from scratch in live mode"). Only the LAST bar's signal
// is returned, matching the Python loop's `signals = engine.step(i)`.
//====================================================================
bool RunReplay(SSignal &outSig)
{
   outSig.valid=false;
   MqlRates m5[];
   int n=CopyRates(_Symbol, PERIOD_M5, 1, InpHistoryM5Bars, m5);
   if(n<50) { Print("RunReplay: not enough M5 history (", n, ")"); return false; }
   if(m5[0].time > m5[n-1].time) ArrayReverse(m5);

   double m15O[],m15H[],m15L[],m15C[]; datetime m15S[],m15E[]; int m15Ready[];
   BuildM15(m5, n, m15S, m15E, m15O, m15H, m15L, m15C, m15Ready);
   int nM15=ArraySize(m15O);
   double atrArr[]; bool atrValid[];
   ComputeATR(m15H, m15L, m15C, nM15, InpAtrPeriod, atrArr, atrValid);

   SPOI pois[]; ArrayResize(pois,0);
   int m15Ptr=0;
   SSignal lastBarSignal; lastBarSignal.valid=false;

   for(int i=0;i<n;i++)
   {
      while(m15Ptr<nM15 && m15Ready[m15Ptr]<=i)
      {
         int j=m15Ptr;
         SCandidate cands[];
         DetectOrderBlocks(j, m15O,m15H,m15L,m15C, atrArr,atrValid, m15S,m15E, cands);
         for(int ci=0; ci<ArraySize(cands); ci++)
         {
            SPOI np;
            np.id = StringFormat("%s_%d", (cands[ci].dir==DIR_LONG?"L":"S"), (long)cands[ci].origin_time);
            np.dir=cands[ci].dir; np.upper=cands[ci].hi; np.lower=cands[ci].lo;
            np.origin_time=cands[ci].origin_time; np.created_time=cands[ci].created_time;
            np.created_m5_index=i; np.strength=cands[ci].strength; np.state=ST_CREATED;
            np.bars_outside=0; np.max_departure_pips=0.0;
            np.departed_time=0; np.departed_index=-1;
            np.retest_time=0; np.retest_index=-1; np.retest_count=0;
            AdmitPOI(pois, np);
         }
         m15Ptr++;
      }

      double hi=m5[i].high, lo=m5[i].low, cl=m5[i].close;
      datetime ts=(datetime)((long)m5[i].time+300);
      double pOpen =(i>0)?m5[i-1].open:0.0,  pHigh=(i>0)?m5[i-1].high:0.0;
      double pLow  =(i>0)?m5[i-1].low:0.0,   pClose=(i>0)?m5[i-1].close:0.0;
      double cOpen=m5[i].open, cHigh=m5[i].high, cLow=m5[i].low, cClose=m5[i].close;
      double depAtr = (m15Ptr>0) ? atrArr[m15Ptr-1] : 0.0;
      bool   depAtrValid = (m15Ptr>0) ? atrValid[m15Ptr-1] : false;

      SSignal barSig; barSig.valid=false;
      int npois=ArraySize(pois);
      for(int k=0;k<npois;k++)
      {
         if(!IsActiveState(pois[k].state)) continue;
         SSignal thisSig; thisSig.valid=false;
         bool trig = (i>=1) && UpdatePOI(pois[k], i, ts, hi,lo,cl,
                                          pOpen,pHigh,pLow,pClose, cOpen,cHigh,cLow,cClose,
                                          m5[i].time, depAtr, depAtrValid, thisSig);
         if(trig && !barSig.valid) barSig=thisSig;
      }
      CompactActive(pois);
      if(i==n-1) lastBarSignal=barSig;
   }
   outSig=lastBarSignal;
   return outSig.valid;
}

//====================================================================
// Risk-gate helpers (mirrors risk.RiskManager)
//====================================================================
void RollDayIfNeeded(datetime ts)
{
   datetime utcTs=(datetime)((long)ts - (long)(InpBrokerUtcOffsetHours*3600.0));
   string day=TimeToString(utcTs, TIME_DATE);
   if(day!=g_lastDay)
   {
      g_lastDay=day;
      g_dayStartEquity=AccountInfoDouble(ACCOUNT_EQUITY);
      g_tradesToday=0;
      g_consecutiveLosses=0;
      g_haltedToday=false;
   }
}

bool InSession(datetime ts)
{
   datetime utcTs=(datetime)((long)ts - (long)(InpBrokerUtcOffsetHours*3600.0));
   MqlDateTime dt; TimeToStruct(utcTs, dt);
   if(dt.day_of_week<1 || dt.day_of_week>5) return false;   // Mon..Fri (MQL5: Sun=0)
   int curMin=dt.hour*60+dt.min;
   int startMin=InpSessionStartHourUTC*60+InpSessionStartMinUTC;
   int endMin=InpSessionEndHourUTC*60+InpSessionEndMinUTC;
   if(startMin<=endMin) return (curMin>=startMin && curMin<endMin);
   return (curMin>=startMin || curMin<endMin);
}

int CountOpenPositions()
{
   int cnt=0;
   int total=PositionsTotal();
   for(int k=0;k<total;k++)
   {
      ulong ticket=PositionGetTicket(k);
      if(ticket==0) continue;
      if(PositionGetString(POSITION_SYMBOL)!=_Symbol) continue;
      if((long)PositionGetInteger(POSITION_MAGIC)!=InpMagic) continue;
      cnt++;
   }
   return cnt;
}

// Scans newly-closed deals for OUR magic/symbol to drive the consecutive-loss
// brake and post-loss cooldown. NB: the Python live path never wires this up
// (RiskManager.on_close is only called from backtest.py) - this port does it
// for real, since that is clearly the intended behaviour per SPECIFICATION.md.
void UpdateClosedTradeStats()
{
   HistorySelect(0, TimeCurrent());
   int total=HistoryDealsTotal();
   for(int i=0;i<total;i++)
   {
      ulong ticket=HistoryDealGetTicket(i);
      if(ticket<=g_lastProcessedDealTicket) continue;
      if((long)HistoryDealGetInteger(ticket,DEAL_MAGIC)!=InpMagic) continue;
      if(HistoryDealGetString(ticket,DEAL_SYMBOL)!=_Symbol) continue;
      if((int)HistoryDealGetInteger(ticket,DEAL_ENTRY)!=DEAL_ENTRY_OUT) continue;

      double profit = HistoryDealGetDouble(ticket,DEAL_PROFIT)
                     + HistoryDealGetDouble(ticket,DEAL_SWAP)
                     + HistoryDealGetDouble(ticket,DEAL_COMMISSION);
      datetime dealTime=(datetime)HistoryDealGetInteger(ticket,DEAL_TIME);
      if(profit<0)
      {
         g_consecutiveLosses++;
         g_cooldownUntilTime=(datetime)((long)dealTime + (long)InpCooldownM5BarsAfterLoss*300);
      }
      else
      {
         g_consecutiveLosses=0;
         g_cooldownUntilTime=(datetime)((long)dealTime + (long)InpCooldownM5BarsAfterAny*300);
      }
      if(ticket>g_lastProcessedDealTicket) g_lastProcessedDealTicket=ticket;
   }
}

ENUM_ORDER_TYPE_FILLING GetFillingMode()
{
   long mode=SymbolInfoInteger(_Symbol, SYMBOL_FILLING_MODE);
   if((mode & SYMBOL_FILLING_IOC)!=0) return ORDER_FILLING_IOC;
   if((mode & SYMBOL_FILLING_FOK)!=0) return ORDER_FILLING_FOK;
   return ORDER_FILLING_RETURN;
}

int VolumeDecimals(double step)
{
   if(step<=0) return 2;
   int d=0; double s=step;
   while(s<0.999999 && d<8) { s*=10.0; d++; }
   return d;
}

void LogVeto(string reason)
{
   PrintFormat("signal vetoed: %s", reason);
}

//====================================================================
// Main per-new-bar entry point (mirrors LiveTrader.poll_once)
//====================================================================
void Process()
{
   SSignal sig;
   if(!RunReplay(sig)) return;

   string gvName = "POIEA_LastTradedEngulf_"+_Symbol+"_"+IntegerToString((int)InpMagic);
   double lastTraded = GlobalVariableCheck(gvName) ? GlobalVariableGet(gvName) : 0.0;
   if((double)sig.engulfTime <= lastTraded) return;   // restart-safety dedupe

   RollDayIfNeeded(sig.signalTime);

   if(!InpIUnderstandTheRisk)
   {
      if(!g_warnedRisk) { Print("Signal found but InpIUnderstandTheRisk=false - no order sent."); g_warnedRisk=true; }
      return;
   }
   long tradeMode=AccountInfoInteger(ACCOUNT_TRADE_MODE);
   if(tradeMode==ACCOUNT_TRADE_MODE_REAL && !InpAllowRealMoneyAccount)
   {
      Print("Signal found but this is a REAL account and InpAllowRealMoneyAccount=false - no order sent.");
      return;
   }

   int openPositions=CountOpenPositions();
   if(openPositions>=InpMaxOpenPositions) { LogVeto("max_open_positions"); return; }
   if(!InSession(sig.signalTime))         { LogVeto("out_of_session"); return; }

   double bid=SymbolInfoDouble(_Symbol,SYMBOL_BID);
   double ask=SymbolInfoDouble(_Symbol,SYMBOL_ASK);
   double spreadPips=(ask-bid)/PipSize;
   if(spreadPips>InpMaxSpreadPips) { LogVeto("spread_too_wide"); return; }

   if(InpMaxTradesPerDay>0 && g_tradesToday>=InpMaxTradesPerDay) { LogVeto("max_trades_per_day"); return; }
   if(g_haltedToday) { LogVeto("daily_loss_halt"); return; }

   double equity=AccountInfoDouble(ACCOUNT_EQUITY);
   if(InpMaxDailyLossPct>0)
   {
      double dd=(g_dayStartEquity-equity)/MathMax(g_dayStartEquity,1e-9);
      if(dd>=InpMaxDailyLossPct) { g_haltedToday=true; LogVeto("daily_loss_halt"); return; }
   }
   if(InpMaxConsecutiveLosses>0 && g_consecutiveLosses>=InpMaxConsecutiveLosses) { LogVeto("consecutive_losses"); return; }
   if(TimeCurrent()<g_cooldownUntilTime) { LogVeto("cooldown"); return; }

   bool longDir=(sig.dir==DIR_LONG);
   double entry = longDir?ask:bid;
   double sl = longDir ? (sig.poiLower-InpSlBufferPips*PipSize) : (sig.poiUpper+InpSlBufferPips*PipSize);
   if(longDir  && sl>=entry) { LogVeto("sl_wrong_side"); return; }
   if(!longDir && sl<=entry) { LogVeto("sl_wrong_side"); return; }

   double slDistance=MathAbs(entry-sl);
   double slPips=slDistance/PipSize;
   double tol=1e-6;
   if(slPips>InpMaxSlPips+tol) { LogVeto(StringFormat("sl_exceeds_max (%.1f pips)",slPips)); return; }
   if(slPips<InpMinSlPips-tol) { LogVeto(StringFormat("sl_below_min (%.1f pips)",slPips)); return; }

   long stopsLevelPoints=SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL);
   double point=SymbolInfoDouble(_Symbol,SYMBOL_POINT);
   if(stopsLevelPoints>0 && slDistance<stopsLevelPoints*point) { LogVeto("below_broker_stops_level"); return; }

   double tp = longDir ? (entry+InpRR*slDistance) : (entry-InpRR*slDistance);

   double tickSize=SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_SIZE);
   double tickValue=SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_VALUE);
   double volMin=SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MIN);
   double volMax=SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MAX);
   double volStep=SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_STEP);

   double riskBudget=equity*InpRiskPerTrade;
   double moneyPerUnit=tickValue/tickSize;
   double lossPerLot=slDistance*moneyPerUnit;
   double denom=lossPerLot+MathMax(InpCommissionPerLotRoundTurn,0.0);
   double lots=0.0;
   if(denom>0) { lots=MathFloor((riskBudget/denom)/volStep + 1e-9)*volStep; lots=MathMin(lots,volMax); }
   if(lots<volMin)
   {
      if(InpRejectIfBelowMinLot) { LogVeto(StringFormat("below_min_lot (calc=%.4f)",lots)); return; }
      lots=volMin;
   }
   lots=NormalizeDouble(lots, VolumeDecimals(volStep));

   int digits=(int)_Digits;
   sl=NormalizeDouble(sl,digits);
   tp=NormalizeDouble(tp,digits);

   MqlTradeRequest req; MqlTradeResult res;
   ZeroMemory(req); ZeroMemory(res);
   req.action=TRADE_ACTION_DEAL;
   req.symbol=_Symbol;
   req.volume=lots;
   req.type=longDir?ORDER_TYPE_BUY:ORDER_TYPE_SELL;
   req.price=longDir?ask:bid;
   req.sl=sl; req.tp=tp;
   req.deviation=InpDeviationPoints;
   req.magic=(ulong)InpMagic;
   req.comment="PoiRetestEngulf";
   req.type_time=ORDER_TIME_GTC;
   req.type_filling=GetFillingMode();

   bool ok=OrderSend(req,res);
   if(!ok || res.retcode!=TRADE_RETCODE_DONE)
   {
      PrintFormat("order_send FAILED retcode=%d comment=%s", res.retcode, res.comment);
      return;
   }
   GlobalVariableSet(gvName, (double)sig.engulfTime);
   g_tradesToday++;
   PrintFormat("ORDER OK dir=%s ticket=%d price=%.5f sl=%.5f tp=%.5f lots=%.2f poi=%s",
               longDir?"LONG":"SHORT", (int)res.order, res.price, sl, tp, lots, sig.poiId);
}

//====================================================================
// Chart dashboard - lightweight status, mirrors run_live.py's printed summary
//====================================================================
void UpdateDashboard()
{
   string armed = InpIUnderstandTheRisk ? "ARMED" : "MONITOR ONLY (InpIUnderstandTheRisk=false)";
   string txt = StringFormat(
      "POI Retest/Engulf EA - %s\n%s\nEquity: %.2f  DayStart: %.2f  TradesToday: %d/%d\nConsecLosses: %d/%d  HaltedToday: %s\nLast bar processed: %s",
      _Symbol, armed, AccountInfoDouble(ACCOUNT_EQUITY), g_dayStartEquity,
      g_tradesToday, InpMaxTradesPerDay, g_consecutiveLosses, InpMaxConsecutiveLosses,
      g_haltedToday?"yes":"no", TimeToString(g_lastProcessedBarTime, TIME_DATE|TIME_MINUTES));
   Comment(txt);
}
//+------------------------------------------------------------------+
