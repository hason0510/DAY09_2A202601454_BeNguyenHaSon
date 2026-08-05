# Member Role Report — Day 9: Multi Agent A2A

## 1. Thông tin cá nhân

| Thông tin       | Nội dung                                          |
| --------------- | ------------------------------------------------- |
| Họ và tên       | Bế Nguyễn Hà Sơn                                  |
| MSSV            | 2A202601454                                       |
| Khóa/Lớp        | K4                                                |
| Vai trò chính   | Toàn bộ hệ thống (nhóm chỉ có một thành viên)     |
| Ngày hoàn thành | 2026-08-05                                        |

## 2. Vai trò và phạm vi công việc

### Phần việc sở hữu

| Module/deliverable | File/hàm phụ trách | Input nhận vào | Output bàn giao | Trạng thái |
| ------------------ | ------------------ | -------------- | --------------- | ---------- |
| Data store + kiểm soát truy cập | `src/store/olist_store.py`, `src/store/views.py` (`load_store`, `build_view`) | 7/9 CSV Olist trong `data/` | Store đã index; `DataView` giới hạn theo capability | Hoàn thành |
| Rule engine EC_POLICY_V2 | `src/policy/rules.py` (`decide_primary`, `decide_secondary`, `apply_policy`, `score_confidence`) | Fact sheet đã gộp | `Verdict`: primary/secondary issue, root cause, responsible party, refund, actions | Hoàn thành |
| Schema + hard gate | `src/policy/schema.py` (`validate`, `cap`), `src/assembler.py` (`build_document`, `repair`) | `Verdict` + fact sheet | JSON đúng schema README mục 6; danh sách vi phạm | Hoàn thành |
| Tầng A2A | `src/a2a/message.py`, `src/a2a/bus.py`, `src/a2a/trace.py` | Envelope giữa các agent | Định tuyến, cưỡng chế scope, `logging/trace.jsonl` | Hoàn thành |
| 8 agent | `src/agents/fact_agents.py`, `reasoning_agents.py`, `verifier_agent.py` | Message A2A | `AgentReport` (facts + evidence + notes) | Hoàn thành |
| Orchestration | `src/pipeline.py` (`Coordinator.run_case`), `run.py` | 50 file `input/EC_*.json` | 50 file `output/EC_*.json`, `logging/metadata.json` | Hoàn thành |
| Audit độc lập | `tools/audit.py` | `output/` + CSV gốc | Báo cáo diff giữa output và giá trị tính lại từ CSV | Hoàn thành |
| Tài liệu | `architecture.md`, `docs/design_notes.md` | — | Sơ đồ agent, quyền truy cập, luồng handoff, lý do thiết kế | Hoàn thành |

### Việc hỗ trợ ngoài phạm vi chính

| Hoạt động | Thành viên/module được hỗ trợ | Kết quả |
| --------- | ----------------------------- | ------- |
| Không có — nhóm một thành viên, không có phần việc bàn giao qua lại | — | — |

## 3. Kết quả theo vai trò

| Nhiệm vụ đã thực hiện | File/hàm/artifact liên quan | Kết quả bàn giao | Cách xác minh |
| --------------------- | --------------------------- | ---------------- | ------------- |
| Khảo sát dữ liệu trước khi code: đo độ phủ join và độ dài mảng thực tế | `data/`, `input/` | 50/50 order resolve được; max 5 item / 3 seller / 4 payment / 10 evidence mỗi case → không case nào chạm array cap | Script recon đọc trực tiếp CSV, chạy trước khi viết `src/` |
| Cài đặt rule engine và chạy toàn bộ 50 case không dùng LLM | `src/policy/rules.py`, `run.py --no-llm` | 50/50 file, 0 vi phạm schema, 1.7s, 0 chi phí API | `python run.py --no-llm` |
| Chạy full pipeline với gpt-4o-mini | `run.py`, `logging/metadata.json` | 50/50 file, 0 hard gate, 0 repair, 150 LLM call, 84.120 token, 35,07s | `python run.py` |
| Audit độc lập không dùng lại code pipeline | `tools/audit.py` | PASS 50/50 | `python tools/audit.py` |
| Ghi trace thật của lượt chạy mới nhất | `logging/trace.jsonl` | 1088 dòng: 400 handoff, 400 report, 150 llm_call, 50 case_start, 50 case_end, 36 critic_findings, 1 run_start, 1 run_end | Đếm event trong file trace |

Nêu một output cụ thể mà phần việc của bạn tạo ra hoặc giúp xác minh:

`tools/audit.py` là artifact tôi coi là có giá trị nhất về mặt xác minh. Nó không import
`src/`, tự đọc lại CSV bằng stdlib và tính lại độc lập mọi giá trị được chấm (tổng tiền,
delivery variance, handoff variance, primary/secondary issue, refund, entity, context,
evidence ID), rồi diff với `output/`. Vì không dùng chung dòng code nào với pipeline, một
lỗi nằm trong `src/` không thể tự che giấu. Kết quả lượt chạy cuối: `PASS -- every graded
value recomputes to the same answer`, 50/50 case.

Giới hạn cần nói rõ: đây là kiểm tra tính nhất quán với dữ liệu nguồn, **không phải đo độ
chính xác so với đáp án**. Repo không có ground truth nên tôi không có cơ sở để tuyên bố
điểm số.

## 4. Giải thích phần kỹ thuật đã thực hiện

### Vấn đề cần giải quyết

Sinh 50 output JSON đúng schema, trong đó mọi con số (tiền làm tròn 2 chữ số, số giờ trễ,
đối soát thanh toán) phải khớp dữ liệu CSV, đồng thời hệ thống phải là multi-agent thật có
phân công và kiểm chứng, và mỗi agent chỉ được dùng model ≤10B tham số.

### Cách triển khai

Xuất phát từ một quan sát về rubric: 6/7 thành phần điểm (85%) là fact kiểm chứng được từ
CSV, chỉ phần phân loại issue mới cần suy luận theo luật. Model 8B-class không cộng và làm
tròn tiền đáng tin cậy 50 lần liên tiếp. Nên tôi đặt ranh giới: **LLM không sinh ra bất kỳ
con số nào được chấm**; toàn bộ số học nằm trong Python.

Để multi-agent vẫn là thật chứ không phải đặt tên cho vui, tôi dùng hai cơ chế:

1. **Quyền truy cập cưỡng chế lúc chạy.** Mỗi agent khai báo `capabilities`; bus dựng
   `DataView` chỉ mở đúng các bảng đó, chạm sai bảng thì ném `ScopeError`. Cụ thể
   `payment_agent` không có scope `items`, nên muốn đối soát nó buộc phải nhận tổng item và
   freight từ `order_product_agent` qua envelope. Handoff trở thành ràng buộc cơ chế, không
   phải quy ước.
2. **Dual-path ở tầng policy.** `rules.apply_policy` (deterministic) ra verdict và giữ thẩm
   quyền; song song đó gpt-4o-mini phân loại độc lập từ cùng fact sheet. Bất đồng được ghi
   trace và trừ `confidence`, nhưng không bao giờ ghi đè verdict.

Hệ quả: LLM chỉ ảnh hưởng đúng một field — `confidence`, field duy nhất README không định
nghĩa cách tính. Mọi field khách quan còn lại là hàm thuần của dữ liệu CSV.

Cây quyết định trong `decide_primary` theo đúng thứ tự ưu tiên bảng README mục 4, dừng ở
rule đầu tiên thỏa. Trường hợp không rule nào khớp được xử lý bằng nhánh fallback có gắn cờ
`used_fallback` và bị trừ confidence — trên bộ 50 case này fallback không kích hoạt lần nào.

### Input, output và contract

| Thành phần | Mô tả |
| ---------- | ----- |
| Input | 50 file `input/EC_*.json` (`case_id`, `customer_request.claimed_order_id`, `investigation_scope`, `policy_version`) + 7 CSV Olist |
| Output | 50 file `output/EC_*.json` theo schema README mục 6; `logging/trace.jsonl`; `logging/metadata.json` |
| Module phụ thuộc | `src/store/olist_store.py` (dữ liệu), `src/llm/client.py` (LLM, optional) |
| Module sử dụng output | `src/agents/verifier_agent.py` và `tools/audit.py` đều đọc lại output để kiểm tra |
| Điều kiện lỗi cần xử lý | Order không có item row (6 case) → 3 field phải null, các mảng rỗng; thiếu `order_delivered_carrier_date` (13 case) → `carrier_handoff_at` null; agent ném exception → bus bắt, case xuống cấp chứ không chết cả run; LLM timeout → retry 3 lần rồi trả `None`, caller đã có sẵn đáp án deterministic |

### Cách xác minh

```bash
python run.py --no-llm          # lõi deterministic, không tốn API
python tools/audit.py           # audit độc lập, không import src/
python run.py                   # full pipeline với gpt-4o-mini
```

- **Kết quả mong đợi:** cả ba lệnh ghi đủ 50 file; 0 vi phạm schema; audit PASS; mọi giá
  trị khách quan giống hệt nhau giữa lượt `--no-llm` và lượt full (chỉ `confidence` khác vì
  nó phụ thuộc mức đồng thuận của adjudicator).
- **Kết quả thực tế:** đúng như trên. Lượt full cuối: 50/50 file, 0 hard gate, 0 case cần
  repair, 0 bất đồng adjudicator, 150 LLM call, 84.120 token, 35,07s. `tools/audit.py` in
  `PASS -- every graded value recomputes to the same answer`.
- **Artifact/log:** `logging/trace.jsonl` (1088 dòng), `logging/metadata.json`, `output/`.
  Không chứa secret; `.env` đã được `.gitignore` chặn và tôi đã kiểm bằng `git check-ignore -v .env`.

## 5. Một quyết định kỹ thuật quan trọng

- **Bối cảnh:** Cần quyết định LLM được phép chạm tới field nào trong output được chấm.
- **Các phương án đã cân nhắc:**
  1. LLM sinh thẳng toàn bộ JSON output, code chỉ validate schema.
  2. LLM quyết định verdict, rule engine đóng vai trò kiểm tra và sửa lại nếu lệch.
  3. Rule engine giữ thẩm quyền tuyệt đối, LLM chạy song song như ý kiến thứ hai, chỉ ảnh
     hưởng `confidence`.
- **Phương án đã chọn:** phương án 3.
- **Lý do:** 85% rubric là số học và join CSV — đó là việc code làm đúng 100%, còn model
  ≤10B thì không. Phương án 1 đặt toàn bộ điểm vào tay model. Phương án 2 nghe hợp lý nhưng
  nếu rule engine đã đủ tin để sửa LLM thì cho LLM quyết trước là thừa rủi ro. Phương án 3
  giữ được tín hiệu kiểm chứng của LLM mà blast radius tối đa chỉ là vài phần trăm ở một
  field README không định nghĩa.
- **Bằng chứng quyết định phù hợp:** trong quá trình build, adjudicator từng bất đồng 18/50
  case; nếu theo phương án 1 hoặc 2 thì 18 case đó đã sai verdict. Truy ngược cả 18 case đều
  ra lỗi prompt của tôi (mục 6), rule engine đúng từ đầu. Ngoài ra `critic_agent` cho thấy
  model còn bịa ra mâu thuẫn trên draft đúng (chi tiết ở `docs/design_notes.md` §3.4), củng
  cố thêm việc không giao field khách quan cho LLM.

## 6. Một lỗi hoặc blocker đã xử lý

- **Triệu chứng/lỗi nguyên văn:** lượt chạy full đầu tiên báo
  `adjudicator disagreements : 18`. Lý do model đưa ra trong trace, lặp lại gần như nguyên
  văn ở nhiều case: `"Order delivered on time and payment reconciles."` — kể cả trên các
  case có `delivery_variance_hours` dương, tức là giao trễ.
- **Lệnh hoặc bước tái hiện:** `python run.py` rồi đọc `logging/trace.jsonl`, lọc event
  `llm_call` có `purpose == "adjudicate"` và đối chiếu với `case_end`.
- **Nguyên nhân gốc:** không phải rule engine sai. Fact sheet tôi gửi cho adjudicator chứa
  `delivered_at`, `estimated_delivery_at` và `delivery_variance_hours` (số thực có dấu), rồi
  kỳ vọng model tự suy ra phép so sánh ngày. Model không đọc dấu của số, mặc định coi mọi
  đơn đã giao là giao đúng hạn. Dấu hiệu nhận ra là bất đồng không rải rác mà tuyệt đối theo
  nhóm: `late_delivery_logistics` sai 10/10, `valid_split_payment` sai 8/8 — một model đoán
  bừa không cho ra pattern sạch như vậy, nên nghi ngờ phải đặt vào phía input của prompt.
- **Cách xử lý:** chuyển phép so sánh về lớp deterministic. `PolicyAgent._fact_sheet` nay
  tính sẵn `delivered_after_estimate`, `has_multiple_payment_rows`, `has_item_rows` và đưa
  boolean cho model; prompt ghi rõ `delivered_after_estimate == true` nghĩa là TRỄ.
- **Cách xác minh sau khi sửa:** chạy lại `python run.py` → `adjudicator disagreements : 0`,
  50/50 file, schema sạch. Hai lỗi cùng loại phát hiện sau đó cũng xử lý theo cách này: model
  đọc `reconciled: null` thành "không có vấn đề thanh toán" (5 bất đồng → 0), và không tự
  biết rule 5 thắng rule 6 khi hai điều kiện cùng đúng.
- **Điều học được:** khi LLM bất đồng hàng loạt và có hệ thống, nghi prompt của mình trước
  khi nghi lớp deterministic. Cả ba lần trong bài này, bất đồng 100% của trọn một nhóm case
  đều là lỗi fact sheet, không phải lỗi model. Hệ quả thiết kế: mọi phép so sánh nên được
  giải sẵn thành boolean trước khi đưa vào prompt; model chỉ nên được giao việc áp dụng thứ
  tự ưu tiên.

## 7. Hiểu biết về luồng end-to-end

Giải thích ngắn gọn bằng lời của bạn:

1. Dữ liệu đi từ Crossref đến vector index như thế nào?
2. Evaluation set và ground-truth document IDs dùng để đo retrieval/answer quality ra sao?
3. Quality checks khác freshness monitoring ở điểm nào trong bài lab?
4. Vì sao phải dùng cùng test set cho baseline, corrupted và repaired?
5. Repair được xem là thành công dựa trên artifact và metric nào?

**Câu trả lời:**

Năm câu hỏi trên nói về Crossref, vector index, retrieval quality và corrupted/repaired
test set — đây là nội dung của một lab RAG khác, không có thành phần nào tương ứng trong
Day 9. Tôi không dựng vector index hay retrieval pipeline nào trong bài này, nên không thể
trả lời trực tiếp mà không bịa. Tôi trả lời phần tương đương của luồng end-to-end bài này:

1. **Dữ liệu đi từ CSV đến output như thế nào.** `run.py` load 7 CSV Olist vào store đã
   index (bỏ `geolocation` và `order_reviews` vì không field nào trong schema output dẫn
   xuất từ chúng). Coordinator đọc `claimed_order_id` từ input, phát envelope cho
   `intake_agent`, rồi tới ba fact agent (customer, order+product, delivery); tổng item và
   freight từ `order_product_agent` được chuyển tiếp sang `payment_agent` để đối soát. Toàn
   bộ fact gộp thành một fact sheet phẳng, đưa vào `policy_agent`, dựng thành document, qua
   `critic_agent` rồi `verifier_agent`, cuối cùng ghi ra `output/EC_xxx.json`.
2. **Không có ground truth nên không đo được accuracy.** Repo chỉ có input và dữ liệu, không
   có đáp án. Thay vào đó tôi kiểm tính nhất quán: `tools/audit.py` tính lại độc lập mọi giá
   trị được chấm từ CSV rồi diff. Đây là điều kiện cần, không phải điều kiện đủ — nếu tôi đọc
   sai luật, cả pipeline lẫn audit sẽ sai giống nhau. Tín hiệu gián tiếp duy nhất tôi có là
   phân bố verdict ra 10/10/8/8/8/6 phủ đều 6 nhánh policy và không case nào rơi vào fallback,
   gợi ý cách đọc luật khớp với ý đồ ra đề.
3. **Verify khác validate ở điểm nào trong bài này.** `verifier_agent` validate cấu trúc:
   schema, array cap, null handling, evidence ID có tồn tại trong CSV không — nó dựng lại tập
   ID hợp lệ từ nguồn chứ không tin draft. `tools/audit.py` thì verify nội dung: tính lại giá
   trị và so sánh. Một draft có thể qua validate mà vẫn sai số, nên cần cả hai.
4. **Vì sao chạy cả `--no-llm` và full trên cùng bộ input.** Để tách biến. Lượt `--no-llm`
   chứng minh mọi giá trị khách quan đã đúng trước khi thêm LLM; lượt full chỉ được phép làm
   khác đi đúng một field là `confidence`. Nếu lượt full lệch ở field nào khác thì đó là dấu
   hiệu LLM đã rò rỉ ra ngoài phạm vi cho phép.
5. **Một lượt chạy được coi là thành công dựa trên artifact nào.** Bốn điều kiện đồng thời:
   `run.py` ghi đủ 50 file và báo `schema: all cases clean` (0 hard gate); `tools/audit.py`
   in PASS; `logging/trace.jsonl` có đủ event của lượt mới nhất; `logging/metadata.json` ghi
   `hard_gate_failures: 0`. Số bất đồng adjudicator là tín hiệu chẩn đoán, không phải điều
   kiện đạt.

## 8. Cam kết của thành viên

Đánh dấu sau khi tự kiểm tra:

- [x] Nội dung báo cáo phản ánh đúng phần việc và mức hiểu của tôi.
- [x] Tôi có thể giải thích luồng end-to-end, không chỉ module mình phụ trách.
- [x] Tôi không ghi "đã chạy thành công" cho phần chưa được kiểm chứng.
- [x] Báo cáo không chứa `.env`, API key, token hoặc secret.
- [x] Báo cáo này không phải bản sao nguyên văn của báo cáo nhóm hoặc báo cáo thành viên khác.

**Họ và tên:** Bế Nguyễn Hà Sơn
**Ngày xác nhận:** 2026-08-05
