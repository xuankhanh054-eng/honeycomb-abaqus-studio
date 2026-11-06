# Kế hoạch chạy toàn bộ chiến dịch H20 — viên bi 2,65 kg, 8 m/s

Ngày lập: 06/11/2026, múi giờ Asia/Bangkok.

**Điều chỉnh theo yêu cầu mới nhất của người dùng:** dùng lại lưới của job Actions
`c45a1d75d6174e31890d9e3861419a9c`: lõi 0,7/1,3 mm, mặt 0,75/1,25 mm,
chiều sâu 1 mm, viên bi 1 mm, bán kính vùng mịn 22 mm, nửa vùng mặt 12 mm,
S4R/S3R và 5 điểm tích phân bề dày. Bỏ các nhánh tinh lưới 0,75/0,5;
lựa chọn này không được ghi thành bằng chứng hội tụ lưới mới. Những bài đang
giải với vùng mịn 25/25 mm giữ nguyên đầu vào và nguồn gốc, chỉ dùng làm chẩn
đoán bổ sung; bộ 400 dùng đồng nhất lưới được người dùng chọn. Các kiểm tra
vật liệu, thời gian, năng lượng, trạng thái hư hỏng và tính đủ của dữ liệu vẫn áp dụng.

Theo lệnh tiếp tục thu đủ 400 mẫu, thu thập FE trên đầu vào đã chốt được phép
chạy song song với các kiểm chứng còn lại. Quyền thu thập này chỉ mở khi vật
liệu, restart và archive restore đã đạt trên đúng source/runtime và đầu vào
khớp hoàn toàn mẫu chuẩn với bốn biến khảo sát. Chưa đạt kiểm chứng toàn chiến
dịch thì chưa cho mẫu vào huấn luyện. Nếu kiểm chứng bị chặn thì dừng phát
mẫu mới; giữ nguyên dữ liệu đã tính để điều tra. Không đổi đầu vào của cấu hình
đã dispatch khi khóa giao thức. Báo riêng số trong 400 đã dispatch, hoàn tất số,
và trạng thái kiểm chứng toàn bộ; pilot và coupon không cộng vào số 400.

Cập nhật thực thi ngày 07/11/2026: **60 OFAT**, gồm 15 mức/biến không trùng
tham chiếu; **259 LHS phát triển**, 1 tham chiếu và 80 test khóa = **400**.
Qualification phục hồi và hạ tầng cả tám tài khoản đã đạt trên worker source
`f7d7e93cb0d97f8c99246a0a904d5d173a19a70d`. Đã bật thu thập song song
và sau đó nâng lên **160 slot Actions (20/tài khoản)** theo yêu cầu khẩn.
160 slot bao gồm job điều phối; khi controller chạy, tối đa 159 worker FE.
Pool FlexNet được ghi riêng là 128 lượt solver 4 CPU theo dữ liệu hiện có;
các analysis có thể chờ token trong hàng đợi giấy phép mặc định của Abaqus.
Controller đối soát và lấp slot tám tài khoản song song, tự gửi lượt kế tiếp
sau 30 giây chờ, có khóa concurrency và không gửi trùng khi đã có lượt chờ;
ngừng nối tiếp khi bị chặn, tắt hoặc hoàn tất. Số mẫu hoàn tất lấy từ receipt
đã kiểm tra, không suy từ việc gửi workflow.

Sửa lỗi biên hình học: bộ lọc đoạn ngắn có thể để lại nắp lõi nằm cách mặt
liên kết dưới 0,05 mm. Với tám đầu vào đã khoanh vùng, liên kết chọn toàn bộ
mặt nắp trong dung sai Tie sẵn có, giữ nguyên mạng vách và khối lượng hình
học; không giảm dung sai cắt hoặc sinh phần tử rất ngắn. Bản worker sửa chỉ
được mở sau khi tám ca đều hoàn thành phép thử Abaqus riêng và xác nhận khối
lượng, bề mặt liên kết, cùng mã vật liệu/solver/restart không đổi. Giữ source
thực tế cho từng kết quả; tiếp tục restart bằng đúng source của checkpoint.

## 1. Phạm vi và đầu ra

Kế hoạch bao phủ kiểm chứng mô hình, pilot, **400 cấu hình ban đầu**, tối đa **80 cấu hình bổ sung**, và **20 cấu hình xác nhận tối ưu**. Tổng dữ liệu chính là **420–500 cấu hình**, chưa tính kiểm tra số và retry. Đây là kế hoạch toàn chiến dịch; việc lập tài liệu và xuất danh sách không bật bộ điều phối hoặc gửi job.

Sau khi có lệnh thực thi toàn bộ, các giai đoạn tự chuyển tiếp khi qua điều kiện kiểm tra. Dừng tại lỗi số, không đủ miền đáp ứng, sai khác hội tụ hoặc hết ngân sách; không yêu cầu xác nhận lặp lại cho các bước đã được cho phép. Không tự thay vận tốc, thời gian dữ liệu chính hoặc vật liệu để vượt điều kiện.

## 2. Khóa cấu hình vật lý

| Hạng mục | Giá trị |
|---|---|
| Tấm sandwich | 100 × 100 mm, lõi tổ ong lõm reentrant hướng hiện tại 0° |
| Chiều cao toàn bộ lõi | **20 mm** |
| Mặt trước / sau | **0,75 / 0,75 mm** |
| Vật liệu lõi và hai mặt | **AA6061-T6 tham khảo**, Johnson–Cook dẻo và hư hỏng |
| Đầu va chạm | **Viên bi cứng D13,1 mm**, R6,55 mm |
| Khối lượng chuyển động | **2,65 kg**, gán tại điểm tham chiếu của cụm cứng |
| Vận tốc | **8 m/s**, va chạm vuông góc tại tâm tấm |
| Động năng đầu | **84,8 J** |
| Thời gian dữ liệu chính dự kiến | **8 ms**; chỉ được khóa sau kiểm tra hội tụ thời gian |
| Gá kẹp / liên kết | Encastre biên / Tie hoàn hảo lõi–mặt |
| Ma sát / khe hở / trọng lực | 0,2 / 0,1 mm / 0 trong bước va chạm |
| Solver | Abaqus/Explicit, double precision, 4 CPU/job, không mass scaling |

D13,1 mm xác định bề mặt tiếp xúc. Khối lượng 2,65 kg là khối lượng hiệu dụng của cụm chuyển động, không phải khối lượng suy từ thể tích viên bi và mật độ kim loại.

Thông số vật liệu, nhiệt, lưới xuất phát và tần suất ghi giữ theo [hồ sơ cấu hình đã chốt](KE_HOACH_CHOT_H20_BI_8MPS.md). Damage evolution dạng chuyển vị 0,1 mm chưa hiệu chuẩn thực nghiệm; kiểm tra độ nhạy 0,05/0,20 mm và ghi rõ phạm vi kết luận là mô hình nhôm tham khảo.

## 3. Bốn biến khảo sát

| Biến | Trường | Khoảng | Tham chiếu | OFAT: bước phía dưới / trên tham chiếu |
|---|---|---:|---:|---:|
| Góc delta | reentrant_delta_deg | 25–45° | 35° | 1,25° / 1,428571° |
| Chiều dày lá lõi | foil | 0,035–0,115 mm | 0,0635 mm | 0,0035625 / 0,007357143 mm |
| Kích thước ô | cell_size | 4,5–8,2 mm | 6,35 mm | 0,23125 / 0,264285714 mm |
| Chiều cao ô lõm | reentrant_cell_height | 3–7 mm | 5 mm | 0,25 / 0,285714286 mm |

Giữ đầy đủ độ chính xác trong CSV; số trong bảng được làm tròn để đọc. Chiều cao ô lõm là biến hình học ô, khác chiều cao toàn bộ lõi H20. Theta không khảo sát; reentrant_indent được suy ra, không thay độc lập. Mọi hình học phải thỏa khe ô > max(8 × foil, 0,5 mm).

OFAT thay một biến mỗi lượt. Latin Hypercube thay phối hợp bốn biến để học tương tác; không có bước nhảy đều. Lấy mẫu có loại miền hình học không hợp lệ, loại trùng theo đầu vào thực tế. Seed phát triển 20261106, kiểm tra 20261107.

Mỗi biến có 8 mức phía dưới và 7 mức phía trên tham chiếu, gồm hai đầu miền và loại chính giá trị tham chiếu. Cấu hình tham chiếu được tính một lần riêng, nên 60 OFAT đều là cấu hình vật lý khác nhau.

## 4. Trình tự chạy

| Giai đoạn | Quy mô | Điều kiện chuyển tiếp |
|---|---:|---|
| A. Kiểm chứng triển khai | 3 bài kéo/nén/cắt; thử checkpoint và phục hồi ở runner khác; preflight 8 repo | Đúng đơn vị, dẻo → damage → xóa; bảo toàn trạng thái; archive phục hồi được; giấy phép và dữ liệu truy cập được |
| B. Pilot tải | **3 cấu hình**, 8 m/s, 8 ms | Đạt kiểm tra số; cấu hình tham chiếu mặt sau nguyên vẹn, đáp ứng phân biệt giữa ba foil |
| C. Hội tụ và độ nhạy | Thời gian 8/12 ms; lưới 1/0,75/0,5; damage 0,05/0,10/0,20 mm | Thời gian sai khác hai mục tiêu ≤2%; lưới ≤5% và cùng phân loại hư hỏng; có báo cáo độ nhạy |
| D. Kiểm tra phủ miền | **40 mẫu phát triển nằm trong 400**, kèm 40 kiểm tra kéo dài đến 12 ms | Cả 40 mẫu đạt hội tụ thời gian và kiểm tra số trên lưới cuối cùng |
| E. Hoàn thành bộ ban đầu | **360 cấu hình còn lại** | Có đủ 320 phát triển + 80 kiểm tra đã lưu và phân loại chất lượng |
| F. Mô hình và bổ sung | 0–80 cấu hình, tối đa 4 đợt × 20 | Đủ số mẫu nguyên vẹn, sai số và độ ổn định Pareto đạt mục tiêu; hoặc hết ngân sách thì báo chưa đạt |
| G. Kiểm tra độc lập | 80 mẫu đã giữ riêng | Mô hình/siêu tham số/đề xuất Pareto đã khóa; báo sai số thật, không dùng nhãn này điều chỉnh tiếp |
| H. Xác nhận tối ưu | **20 cấu hình mới** | FE xác nhận hai mục tiêu, khối lượng, mặt sau nguyên vẹn và chất lượng số |
| I. Bàn giao | Một bộ dữ liệu và báo cáo tập trung | Kiểm tra checksum, chỉ mục, số lượng, khả năng tải và phục hồi |

### B. Ba pilot cụ thể

| Pilot | Foil | Delta | Cell size | Chiều cao ô lõm | Vận tốc | Thời gian |
|---|---:|---:|---:|---:|---:|---:|
| P1 | 0,035 mm | 35° | 6,35 mm | 5 mm | 8 m/s | 8 ms |
| P2 — tham chiếu | **0,0635 mm** | 35° | 6,35 mm | 5 mm | 8 m/s | 8 ms |
| P3 | 0,115 mm | 35° | 6,35 mm | 5 mm | 8 m/s | 8 ms |

Cửa sổ đáp ứng nghiên cứu: chuyển vị cực đại mặt sau của P2 khoảng **1–3 mm**, ưu tiên gần 2 mm; mặt sau không bị xóa phần tử; chênh lệch đáp ứng ba foil vượt sai số số. Đây không phải tiêu chuẩn an toàn. Nếu không đạt, giữ kết quả, dừng và đề xuất điều chỉnh; không chạy thêm vận tốc khác.

### C–D. Thời gian và số lượt kiểm tra

- **8 ms là thời gian mục tiêu của bộ chính. 12 ms chỉ dùng kiểm tra**, không tự đổi toàn bộ bộ chính sang 12 ms.
- Tại 8 ms phải kết thúc tiếp xúc và đáp ứng/năng lượng gần ổn định; so với kết quả 12 ms, cả SEA_core và chuyển vị mặt sau thay đổi ≤2%, cùng phân loại hư hỏng.
- Kiểm tra ba foil trên lưới cuối được chọn. Nếu lưới cuối khác lưới pilot, chạy lại phép so sánh thời gian tương ứng, không ghép kết quả khác lưới để kết luận hội tụ thời gian.
- Lưới: ba mức trên ba foil, tối đa 9 bài; tái sử dụng ba pilot nếu hợp đồng mô hình trùng hoàn toàn. Chọn mức thô nhất thỏa sai khác ≤5% so với mức tiếp theo, cùng phân loại hư hỏng.
- Damage sensitivity: ba mức trên ba foil, 9 bài so sánh; mức 0,10 mm có thể tái sử dụng nếu trùng hoàn toàn. Báo độ nhạy, không chỉnh thông số chỉ để tạo hình ảnh rách.
- Kiểm tra thời gian trên 40 mẫu phát triển phủ miền trước khi phát 360 mẫu còn lại. 40 bản kéo dài chỉ là kiểm tra số, không tính thành 40 mẫu huấn luyện mới.
- Nếu 8 ms chưa đủ, dừng báo cáo để quyết định giao thức mới; không âm thầm trộn mẫu 8 và 12 ms.

## 5. Danh sách 400 cấu hình ban đầu

| Nhóm | Số cấu hình | Phương pháp |
|---|---:|---|
| Tham chiếu | 1 | Delta35°, foil0,0635, cell6,35, chiều cao ô5 |
| OFAT phát triển | 60 | 15 mức cho từng biến, giữ ba biến còn lại ở tham chiếu |
| LHS phát triển | 259 | Phủ miền khả thi, thay phối hợp bốn biến |
| Kiểm tra giữ riêng | 80 | LHS độc lập; không dùng để chọn mô hình, siêu tham số hoặc điểm bổ sung |
| **Tổng** | **400** | **320 phát triển + 80 kiểm tra** |

Danh sách từng lượt đã xuất tại:

`C:/UET/DO_AN/output/core-damage-v2-20261107/planned-400-h20-sphere-8mps-8ms-ofat60.csv`

CSV có mã cấu hình, nhóm, phương pháp, giai đoạn 40/360, repo, các trường thay đổi, bốn biến, indent suy ra, các thông số cố định và hash đầu vào. Trường lưới và thời gian còn là ứng viên cho đến khi qua kiểm tra; sau khi khóa phải cập nhật danh sách và hash trước khi dispatch.

Không bổ sung trước tọa độ cho 80 mẫu thích nghi và 20 điểm xác nhận: các điểm này phụ thuộc kết quả FE và mô hình, cần chọn khi có bằng chứng.

## 6. Phân phối 8 repo

Mỗi repo nhận 40 phát triển và 10 kiểm tra. 40 mẫu kiểm tra phủ miền đã chọn theo maximin thuộc nhóm phát triển, phân bổ như bảng dưới; không ép đều làm thay đổi tiêu chí chọn điểm.

| Chủ sở hữu repo honeycomb-abaqus-studio | Phát triển | Kiểm tra giữ riêng | Trong 40 mẫu phủ miền | Tổng ban đầu |
|---|---:|---:|---:|---:|
| luukhanh24 | 40 | 10 | 8 | 50 |
| KhanhStark | 40 | 10 | 4 | 50 |
| xuankhanh24 | 40 | 10 | 5 | 50 |
| dzdeptrai390 | 40 | 10 | 3 | 50 |
| khanhstarkpubg-ux | 40 | 10 | 9 | 50 |
| xuankhanh054-eng | 40 | 10 | 6 | 50 |
| starkdzdz-eng | 40 | 10 | 1 | 50 |
| luukhank574-design | 40 | 10 | 4 | 50 |
| **Tổng** | **320** | **80** | **40, đã nằm trong 320** | **400** |

### Cấp slot và chạy nối

- Hàng đợi bền vững tại repo xuankhanh24, xác thực qua GitHub App đã được cấu hình; cùng source/runtime và manifest đã khóa trên tám repo.
- Một cấu hình có một solver job đang tính tại một thời điểm. Slot trống nhận cấu hình kế tiếp, không đợi cả nhóm kết thúc.
- Tăng tải **8 → 20 → 40 → 80 → trần đã kiểm chứng**, tối đa danh nghĩa 160 job trên tám chủ sở hữu. Mỗi chủ sở hữu Free có giới hạn 20 job đồng thời; phải trừ controller, prepare/finalize và workflow khác. Công suất solver thực tế còn phụ thuộc giấy phép, RAM và I/O. [Giới hạn GitHub Actions](https://docs.github.com/en/actions/reference/limits).
- Runner hosted có trần 6 giờ/job. Gửi yêu cầu ngừng solver có kiểm soát trước phút 240 tính từ lúc runner bắt đầu; timeout job 330 phút, để dành thời gian lưu và upload. Khôi phục bằng Explicit restart đã xác minh checksum và danh tính mô hình. [Giới hạn GitHub Actions](https://docs.github.com/en/actions/reference/limits).
- Tối đa 4 đoạn/cấu hình; hết ngân sách thì giữ checkpoint và báo điều tra. Bảo toàn thời gian tích lũy, vận tốc, damage và năng lượng; không khởi tạo lại va chạm khi chạy nối.
- Lỗi tải/cài/upload có retry riêng. Upload lỗi dùng lại kết quả đã tính. Lỗi số tối đa hai lần retry, sau đó cách ly; không tự đổi vật liệu, tải, lưới.
- Bộ điều phối chạy trên GitHub; máy chủ giấy phép vẫn cần hoạt động và truy cập được trong suốt đợt tính.

## 7. Dự đoán, bổ sung và tối ưu

### Hai mục tiêu và ràng buộc

1. Tăng **SEA_core = (ALLPD_core + ALLDMD_core) / khối lượng lõi ban đầu**.
2. Giảm chuyển vị xuống cực đại trên **toàn bộ mặt sau**, xét mọi nút còn liên kết và mọi frame, trừ trạng thái ban đầu.
3. Khối lượng lõi ≤ cấu hình tham chiếu H20 mới; mặt sau không bị xóa phần tử.

Xuất riêng năng lượng đàn hồi, ma sát, động năng và năng lượng nhân tạo; không cộng chúng vào SEA_core. Năng lượng được lấy riêng cho lõi, từng mặt và toàn mô hình. [Abaqus: Total Energy Output](https://docs.software.vt.edu/abaqusv2025/English/SIMACAEOUTRefMap/simaout-c-exp-totalenergyoutput.htm).

### Huấn luyện và vòng bổ sung

- So sánh Ridge, hồi quy bậc hai, Gaussian Process và Extra Trees bằng kiểm định chéo 5 phần trên nhóm phát triển. Chuẩn hóa và chọn siêu tham số bên trong vòng kiểm định; dữ liệu kiểm tra độc lập không tham gia.
- Dùng NSGA-II tìm Pareto trên mô hình dự đoán, áp dụng ràng buộc khối lượng và hư hỏng mặt sau.
- Tối đa **4 đợt × 20 mẫu**. Mỗi đợt: **10 bất định cao + 6 gần Pareto + 4 miền thưa**; tổng tối đa 40/24/16. Loại trùng; ràng buộc hình học vẫn giữ nguyên. Chạy FE, kiểm tra số, cập nhật mô hình rồi chọn đợt kế tiếp.
- Dừng bổ sung khi có ≥250 mẫu phát triển/bổ sung nguyên vẹn, CV R² ≥0,90 và NRMSE ≤10% cho từng mục tiêu, và Pareto ổn định. NRMSE chuẩn hóa bằng P95–P5 của đáp ứng; Pareto dùng sai khác front chuẩn hóa ≤0,05 giữa các vòng cập nhật.
- Hết 80 mẫu vẫn chưa đạt thì báo chưa đủ; không mở test để tìm cách điều chỉnh mô hình.
- Sau khi khóa mô hình và cách chọn phương án, đánh giá 80 mẫu test một lần. Không đạt thì báo kết quả kiểm tra độc lập và giới hạn sử dụng; không tự tiếp tục huấn luyện trên test.

### 20 FE xác nhận

Chọn 20 cấu hình mới, không trùng bộ trước: 2 cực trị Pareto, 6 điểm vùng cân bằng và 12 điểm phân bố dọc front. Nếu các nhóm trùng, thay bằng điểm mới ở phần front chưa được phủ. Chạy cùng vật liệu, tải, lưới và thời gian đã khóa; báo sai số dự đoán so với FE cho từng điểm. Loại phương án vi phạm khối lượng, mặt sau nguyên vẹn hoặc chất lượng số. Chỉ công bố Pareto đã được FE xác nhận.

## 8. Chất lượng, dữ liệu và tiến độ

### Kiểm tra chất lượng mỗi lượt

- Solver hoàn tất đủ thời gian, lịch sử liên tục và đủ trường damage.
- Động năng ban đầu khớp 84,8 J trong dung sai 0,1%; cân bằng năng lượng lệch ≤5% động năng đầu.
- |ALLAE|/|ALLIE| ≤5% ở các thời điểm nội năng đã đạt ít nhất 1% động năng đầu; kiểm tra theo thời gian, không lấy tỷ số của hai cực đại khác thời điểm.
- Ghi riêng solver lỗi, kết quả không đạt kiểm tra số, phá hủy vật lý và mẫu mặt sau nguyên vẹn đủ điều kiện hồi quy.
- Nút/phần tử mất liên kết không được làm xuất hiện chuyển vị nhỏ giả; hư hỏng mặt sau luôn có cờ riêng.
- Chỉ hoàn tất sau khi upload và xác minh checksum. Retry, chạy nối, bài hội tụ không tăng số mẫu độc lập.

### Thư mục bàn giao

```text
campaign-h20-sphere-8mps/
  samples.csv
  data_dictionary.md
  manifest.json
  material.json
  quality_report.csv
  archive_index.csv
  curves/<configuration_id>/impact_history.csv
  models/<configuration_id>/... INP, ODB, log, checkpoint hoặc chỉ mục archive
  reports/... kiểm tra số, sai số mô hình, Pareto, 20 FE xác nhận
```

samples.csv ghi một dòng cho một cấu hình: cả bốn biến, thông số cố định, nhóm, tham số thay đổi, khối lượng ban đầu, lưới/thời gian, mục tiêu, cờ hư hỏng, chất lượng, nguồn repo, source/runtime/hash và lịch sử retry. Phân loại OFAT/LHS/adaptive/confirmation rõ ràng. Giữ nhãn của 80 test ngoài quyền truy cập của quá trình huấn luyện/chọn điểm cho đến khi mở test.

### Tiến độ và ETA

Báo riêng: số cấu hình đã gửi, đang tính, có checkpoint, tính xong, archive đã xác minh, đạt kiểm tra số, mặt sau nguyên vẹn và dùng huấn luyện. Tỷ lệ của bộ ban đầu dùng mẫu số 400; pilot/kiểm chứng báo riêng. Khi thêm dữ liệu, hiển thị thêm tiến độ tổng theo quy mô thực tế để không nhầm mẫu số.

ETA được tính sau pilot từ thời gian build/solve/upload thực đo, phân bố thời gian theo hình học, tỷ lệ retry và slot hoạt động. Cập nhật theo các lượt hoàn tất; không suy thời gian runner từ con số vật lý 8 ms và không hứa thời hạn theo 160 slot chưa được kiểm chứng.

## 9. Các việc phần mềm phải hoàn tất trước khi bật chạy toàn bộ

1. Giữ controller tắt trong khi đổi catalog; lưu lịch sử các lượt cũ, không gán lại bằng chứng cũ cho mô hình H20 mới.
2. Commit/publish source mới và chạy qualification đúng source/runtime; cập nhật pin worker/controller tương ứng trên tám repo. Kiểm tra hiện có chưa thay cho bằng chứng FE của source mới.
3. Đã triển khai khóa **vận tốc cố định 8 m/s** cho đường chạy toàn bộ và bỏ vận tốc fallback. Cần qualification đúng source trước khi dùng.
4. Đã triển khai **8 ms là thời gian chính**, 12 ms là diagnostic; kiểm tra không đạt sẽ dừng để điều tra. Lưới được chọn phải có kiểm tra 8/12 ms riêng trước độ nhạy hư hỏng.
5. Đổi execution_scope từ pilot sang full chỉ khi có lệnh thực thi toàn bộ và các điều kiện phần mềm trên đã được xác minh. Trong lúc chỉ lập kế hoạch, scope vẫn là pilot và enabled vẫn false.
6. Bảo đảm metadata pilot ghi foil là biến thay đổi giữa ba lượt; vận tốc 8 m/s thuộc nhóm cố định. Khóa schema danh sách 400 và cổng mở nhãn test.
7. Chạy thử mất kết nối/upload lại, restart controller, phục hồi runner khác và chống trùng trước khi mở rộng công suất.
8. Đã triển khai tối đa bốn đợt 20 mẫu, mỗi đợt 10 uncertainty + 6 Pareto + 4 sparse, có đánh giá lại sau mỗi đợt. Bộ chọn 20 điểm xác nhận loại cấu hình đã biết/trùng, giữ hai cực trị, sáu điểm thỏa hiệp và mười hai điểm phủ khoảng trống mục tiêu. Các bước này vẫn phải đi qua kiểm tra tích hợp và khóa nhãn test như kế hoạch.

### Tình trạng kiểm chứng cơ chế dừng

Kiểm thử phần mềm tại commit `ec09e28c`: 336 passed, 1 skipped trên Windows; Ruff và ty đạt. Kiểm thử không thay thế qualification Abaqus.

- [Run 42240714325](https://github.com/xuankhanh24/honeycomb-abaqus-studio/actions/runs/42240714325): không callback/4 luồng hoàn tất; callback tối giản/1 CPU dừng sạch; callback tối giản/4 luồng lỗi bộ nhớ. MPI chưa khởi động solver vì thiếu lệnh ssh.
- [Run 42244271427](https://github.com/xuankhanh24/honeycomb-abaqus-studio/actions/runs/42244271427): TLS, đổi thời điểm yêu cầu dừng và XPLB_EXIT đều vẫn lỗi bộ nhớ ở 4 luồng; XPLB_EXIT/1 CPU không có dấu crash. Workflow chẩn đoán xanh chỉ xác nhận đã thu log, không chứng nhận cơ chế dừng đạt.
- [Run 42246374349](https://github.com/xuankhanh24/honeycomb-abaqus-studio/actions/runs/42246374349): đang kiểm tra MPI cục bộ/4 tiến trình, yêu cầu dừng chỉ từ luồng chính và parallel=loop. Chưa có kết quả tại thời điểm ghi.

- Run 42247330558: hai biến thể kết thúc bước hết thời gian chờ; bị loại.
- Run 42250805080: XPLB_EXIT từ luồng chính dừng sạch nhưng mất lịch sử gốc; bị loại. Bộ đọc ODB đã được sửa để kiểm tra danh sách khóa khi trường không tồn tại.
- [Run 42253210205](https://github.com/xuankhanh24/honeycomb-abaqus-studio/actions/runs/42253210205): **đạt**, source `ca9ee5448fb312c7d7ed9fac5b4eefde757697b8`. MPI/DOMAIN/4, double=both; phục hồi đàn hồi giữ nguyên 11 kênh lịch sử. Mẫu shell đang mềm hóa có PEEQ≈0,424, SDEG≈0,402, JCCRT=1, TEMP≈79,24 °C, STATUS=1 và vận tốc khác 0 tại checkpoint. Runner thứ hai giữ nguyên trạng thái, chạy tới xóa phần tử và đủ 60 ms, 19 kênh lịch sử liên tục.
- Cả bảy repo phụ đã đạt kiểm tra hạ tầng đúng source; repo chính đạt qua workflow trên. Runtime hash MPI: `f43a4a1f6e58128e11b0ef13e1494ccb7825687f9b58111abf3f8cbf751f92b2`.

Đã có xóa phần tử trong các bài thử vật liệu kéo/nén/cắt. Chưa có bằng chứng phá hủy panel H20 hợp lệ tại thời điểm mở pilot. Tham số phá hủy vẫn chưa được hiệu chuẩn thực nghiệm. Campaign `core-damage-v2-20261107` đã đặt `execution_scope=full`, `enabled=true`, pha `pilot`; các cổng thời gian, lưới, độ nhạy và audit 40 vẫn khóa dữ liệu chính. [Lượt controller mở pilot](https://github.com/xuankhanh24/honeycomb-abaqus-studio/actions/runs/42256510146).

**Tóm tắt:** kiểm chứng → 3 pilot → hội tụ/độ nhạy → 40 mẫu trong bộ 400 → 360 mẫu còn lại → tối đa 80 bổ sung → kiểm tra độc lập → 20 FE xác nhận → dữ liệu và báo cáo tập trung.
