# Design notes — vì sao kiến trúc này, và cách dựng lại nó

Tài liệu này giải thích **lý do** đằng sau từng quyết định, kèm số liệu đo được.
`architecture.md` mô tả hệ thống *là gì*; file này ghi lại *tại sao nó thành ra như vậy*
và những gì cần biết nếu muốn làm lại từ đầu.

---

## 1. Quan sát mở đầu định hình toàn bộ thiết kế

Trước khi viết dòng code nào, tôi đọc rubric ở README mục 8:

| Thành phần | Trọng số | Bản chất |
| --- | ---: | --- |
| Primary/secondary issues | 15% | suy luận theo luật |
| Affected entities | 15% | join CSV |
| Customer/product context | 15% | join CSV |
| Delivery analysis | 15% | số học trên timestamp |
| Payment reconciliation | 15% | số học trên tiền |
| Root cause + evidence | 15% | tra bảng + join CSV |
| Financial resolution + actions | 10% | số học + tra bảng |

**85% điểm là fact kiểm chứng được, không phải phán đoán.** Đề lại giới hạn model
≤10B tham số. Một model 8B-class không cộng nổi 5 dòng `price` rồi làm tròn 2 chữ số
một cách đáng tin cậy 50 lần liên tiếp.

Từ đó ra nguyên tắc trung tâm: **LLM không được sinh ra bất kỳ con số nào được chấm.**
Nhưng đề cũng nói rõ *"không có điểm cho việc chỉ đặt tên nhiều agent nhưng toàn bộ
xử lý nằm trong một prompt duy nhất"*. Vậy multi-agent phải là thật, chỉ là phân công
lại: agent chia miền dữ liệu, bàn giao bằng chứng, kiểm chứng chéo — còn số học nằm
trong Python.

## 2. Ba quyết định kiến trúc chính

### 2.1 Quyền truy cập là ràng buộc lúc chạy, không phải chú thích trên sơ đồ

Mỗi agent khai báo `capabilities`; bus dựng `DataView` giới hạn đúng các bảng đó.
Chạm sai bảng → `ScopeError`.

Hệ quả thiết kế quan trọng nhất: `payment_agent` **không có** scope `items`. Nên để
đối soát được, nó buộc phải nhận tổng item/freight từ `order_product_agent` qua
envelope. Handoff trở thành **bắt buộc về mặt cơ chế**, không phải quy ước lịch sự.
Nếu cho cả hai agent đọc chung store, "handoff" chỉ còn là hình thức.

### 2.2 Rule engine là thẩm quyền, LLM là ý kiến thứ hai

`src/policy/rules.py` là hàm thuần: fact sheet vào, verdict ra. `policy_agent` chạy nó,
rồi **song song** hỏi gpt-4o-mini phân loại độc lập từ cùng fact sheet. Bất đồng được
ghi trace và trừ `confidence`, nhưng **không bao giờ ghi đè** verdict.

Đây là mô hình dual-path: có được tín hiệu kiểm chứng của LLM mà không nhận rủi ro
hallucination lên field được chấm.

### 2.3 Blast radius của LLM đúng bằng một field

`confidence` là field duy nhất README không định nghĩa cách tính, nên nó là chỗ duy
nhất an toàn để phản ánh mức đồng thuận giữa agent. Mọi field khác — kể cả
`case_status` và `resolution_actions` — do rule engine quyết. Một cú hallucination
tệ nhất cũng chỉ làm lệch `confidence` vài phần trăm.

---

## 3. Những gì tôi đo được về gpt-4o-mini

Phần này là kết quả thực nghiệm trong lúc build, không phải phỏng đoán. Đây cũng là
lý do một số agent có hình dạng hiện tại.

### 3.1 Model không tự suy ra được phép so sánh — phải đưa boolean tính sẵn

Bản đầu, fact sheet gửi cho adjudicator gồm `delivered_at`, `estimated_delivery_at`
và `delivery_variance_hours` (số có dấu). Kết quả:

- **18/50 case bất đồng**, và pattern là tuyệt đối: `late_delivery_logistics` 10/10,
  `valid_split_payment` 8/8 — nghĩa là *toàn bộ* hai nhóm.
- Lý do model đưa ra luôn là *"Order delivered on time and payment reconciles"*,
  ngay cả khi `delivery_variance_hours` **dương** (tức là trễ).

Model không đọc dấu của số. Sửa: đưa thẳng `delivered_after_estimate: true/false` đã
tính sẵn trong Python, và ghi rõ trong prompt rằng `true` nghĩa là TRỄ.

→ **18 bất đồng → 0.**

Bài học: việc so sánh thuộc về lớp deterministic. Model chỉ nên áp dụng *thứ tự ưu tiên*,
đừng bắt nó vừa tính vừa suy luận.

### 3.2 `null` bị đọc thành "không có vấn đề"

Sau khi thêm `freight_total_brl` vào fact sheet, adjudicator lệch 5 case — đều là các
đơn `unavailable` không có item row, nên `reconciled = null`. Model diễn giải:
*"reconciled is null, indicating no payment issues"* rồi nhảy sang rule 6, dù rule 2
(`unavailable` + payment > 0) không hề nhìn tới `reconciled`.

Sửa: nói thẳng trong prompt rằng rule 1/2 không xét `reconciled`, và `null` chỉ có
nghĩa "đơn không có item row".

→ **5 bất đồng → 0.**

### 3.3 Precedence chồng lấn phải được nêu tường minh

`valid_split_payment` (rule 5) và `unsupported_late_claim` (rule 6) có thể cùng đúng:
giao đúng hạn + reconciled + ≥2 payment row. Bảng README xếp rule 5 trước nên rule 5
thắng — nhưng model chọn 50/50 giữa hai đáp án (4/8 case sai).

Sửa: viết hẳn vào prompt rằng rule 5 thắng rule 6, kèm lý do ngữ nghĩa ("rule 5 nói về
*cách khách trả tiền*, không phải *lúc hàng tới*").

### 3.4 Critic agent: đo được là không hội tụ

`critic_agent` phản biện draft. Tôi lặp 5 phiên bản prompt, đo số case bị nêu finding:

| Vòng | Thay đổi | Case có finding |
| -: | --- | ---: |
| 1 | fact sheet thô | 39/50 |
| 2 | + boolean tính sẵn | 29/50 |
| 3 | + bảng policy, lọc theo `fact_key` | **11/50** |
| 4 | + kênh "consistent", + freight total | 21/50 |
| 5 | thu hẹp về field phân loại | 36/50 |

Không hội tụ. Tệ hơn, các finding sống sót là **đồng ý được diễn đạt như phản đối**:

- `"Draft states late delivery, but order was delivered late"`
- `"Refund should be full payment total, but is correct"`
- `"Refund should be full payment total, not 85.14"` — trong khi 85.14 **chính là**
  payment total của case đó
- `"Full refund is correct for unavailable order, no contradiction"` — nằm trong
  mảng `findings`

Hai nguyên nhân gốc:

1. **Model không so sánh được hai con số ở hai vị trí khác nhau trong prompt.** Đây
   đúng là lý do ban đầu tôi đặt mọi số học vào lớp deterministic — nó tái xuất hiện
   ở đây.
2. **Khung "hãy tìm mâu thuẫn" tạo bias phải sinh ra output.** Cho một draft đúng,
   model vẫn chế ra bất đồng để lấp mảng.

**Quyết định:** giữ `critic_agent` chạy trên mọi case và ghi toàn bộ vào trace (giá
trị audit thật), nhưng **bỏ hoàn toàn ảnh hưởng của nó lên `confidence`**. Một tín
hiệu có false-positive 72% không được phép chạm vào field được chấm.

Tôi giữ agent thay vì xoá vì trace của nó là bằng chứng cho chính kết luận này, và
vì nó không tốn gì ngoài ~1.9k token/run.

### 3.5 Đối chiếu: adjudicator là tín hiệu thật

Ngược lại, adjudicator **có** giá trị: 18 bất đồng ban đầu, mỗi cái truy ra một lỗi
prompt/fact-sheet thật (3.1–3.3). Sau khi sửa, nó đồng ý 50/50.

Điểm cần trung thực: ở run cuối, một tín hiệu đồng ý 100% không mang thêm thông tin
mới. Giá trị của nó thể hiện **trong quá trình phát triển** như một regression check,
không phải ở con số cuối cùng.

---

## 4. Những chỗ đề bỏ ngỏ và lựa chọn của tôi

| Điểm mơ hồ | Lựa chọn | Lý do |
| --- | --- | --- |
| `category_names` tiếng Bồ hay tiếng Anh? | Giữ `product_category_name` gốc (`beleza_saude`) | README mục 5 yêu cầu evidence "dựng trực tiếp từ dữ liệu"; mọi field khác đều là giá trị raw từ CSV. File translation có mặt chỉ vì thuộc bộ 9 file gốc. |
| `item_total_brl` khi đơn không có item? | `0.0` | README mục 4 liệt kê **đúng ba** field phải null (`expected_total_brl`, `difference_brl`, `reconciled`). Tôi theo sát chữ nghĩa thay vì suy rộng. |
| Cách tính `confidence`? | Base theo loại verdict + điều chỉnh theo chất lượng bằng chứng và đồng thuận | Hàm minh bạch trong `rules.score_confidence`, thay vì để LLM bịa một con số. |
| Danh sách hard gate? | Coi mọi cam kết cấu trúc README có nêu là gate | Fail lớn tiếng còn hơn nộp một case bị chấm 0. |
| `affected_entities.seller_ids` = tất cả seller hay chỉ seller vi phạm? | Tất cả seller của đơn (cap 3) | "Affected" ≠ "responsible". Seller chịu trách nhiệm chỉ xuất hiện trong `evidence_ids` và `responsible_parties`, đúng README mục 5. |
| Action bổ sung áp dụng khi nào? | `review_seller_handoff`/`review_carrier_delay` theo primary issue; `verify_refund_completion` khi refund > 0; `coordinate_multi_seller_case` khi có `multi_seller_order`; `verify_payment_allocation` khi có `split_payment` và primary ≠ `valid_split_payment` | README chỉ nêu thứ tự, không nêu điều kiện. Đây là cách đọc tự nhiên nhất và nhất quán với ngoại lệ mà README có nêu rõ. |

## 5. Xác nhận trên dữ liệu thật

Recon trước khi code (`50 case`, join với CSV):

- 50/50 order tồn tại; customer, payment, product, seller đều resolve 100%.
- Max **5 item / 3 seller / 4 payment / 10 evidence** mỗi case → **không case nào chạm
  array cap**. Logic cap chỉ là lưới an toàn.
- Phân bố verdict: `late_delivery_seller` 10, `late_delivery_logistics` 10,
  `canceled_order_paid` 8, `valid_split_payment` 8, `unsupported_late_claim` 8,
  `unavailable_order_paid` 6 — **cân bằng đều 6 nhánh, 0 case rơi vào fallback**.
  Bộ đề rõ ràng được thiết kế để phủ đều policy, và cách đọc luật của tôi khớp với
  ý đồ đó.
- 6 case không có item row (`EC_012, 031, 033, 034, 035, 043`, đều `unavailable`) →
  đúng nhánh null handling.
- 13 case thiếu `order_delivered_carrier_date` → `carrier_handoff_at = null`.

Kết quả run cuối:

```
50/50 output       0 hard gate     0 repair     0 adjudicator disagreement
150 LLM call       84,120 token    35s          trace 1088 dòng
tools/audit.py:    PASS 50/50 (audit độc lập, không import src/)
```

## 6. Nếu cần làm lại

Thứ tự dựng lại, mỗi bước tự kiểm chứng được:

1. `src/store/olist_store.py` — load + index. Kiểm: `store.stats` ra 99441 order.
2. `src/policy/rules.py` — cây quyết định. Kiểm: phân bố phải ra **8/10/10/8/8/6**,
   0 fallback. Sai phân bố nghĩa là đọc sai luật.
3. `src/policy/schema.py` + `src/assembler.py` — dựng document và gate.
4. `run.py --no-llm` — phải ra đủ 50 file, 0 vi phạm schema, **không tốn API**.
   Đây là chốt chặn quan trọng nhất: mọi giá trị được chấm phải đúng *trước khi*
   thêm LLM.
5. `tools/audit.py` — audit độc lập. Phải PASS trước khi bật LLM.
6. Thêm các LLM agent. Kiểm: adjudicator phải tiến về 0 bất đồng; nếu không, lỗi
   gần như chắc chắn nằm ở fact sheet (thiếu boolean tính sẵn) chứ không ở rule engine.

Bẫy đáng nhớ nhất: **khi LLM bất đồng hàng loạt và có hệ thống, hãy nghi prompt của
mình trước khi nghi rule engine.** Cả ba lần trong dự án này, bất đồng 100% của trọn
một nhóm case đều là lỗi fact sheet của tôi, không phải lỗi model.

## 7. Chi phí

150 call/run, ~84k token, ~35 giây, dưới 0.02 USD mỗi lần chạy full 50 case.
`--no-llm` chạy 1.7 giây, chi phí 0.
