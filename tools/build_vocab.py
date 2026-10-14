import json,re
from collections import Counter
c=Counter()
for l in open('vocab_rows.jsonl',encoding='utf-8'):
    d=json.loads(l)
    for w in re.findall(r"[^\W\d_]+",(d['name'] or '').lower()): c[w]+=1
# extra common menu words
extra="""hải sản khai vị món cá biển cơm mỳ mì đồ uống thức giải khát nước trà cà phê sữa đá nóng lạnh chiên xào nướng hấp luộc rang kho
tái chanh ốc móng tay trứng bò gà heo lợn vịt dê ếch tôm mực cua ghẹ nghêu sò hàu chả nem bún phở cháo lẩu canh gỏi nộm rau muống cải tỏi
sả ớt gừng me muối tiêu xanh giấy bạc bánh tráng cuốn dương châu thập cẩm đặc biệt thường lớn nhỏ vừa ly chai lon phần suất tô dĩa đĩa
bia sinh tố ép cam dừa chanh dây đường đen trân châu kem chè sữa chua thêm topping set combo menu thực đơn bảng giá bò húc khoai tây
xúc xích lạc đậu khuôn bắp sách bì quả khổ qua chà bông bầu chấm tóp mỡ cháy hành cần chua ngọt nắng thu đốm bạc má trích giòn mắm
thịt sườn ba chỉ nạc vai lòng dồi tiết gan cật óc chân giò móng giò cánh đùi ức sụn bì nạm gầu gân tái chín viên mọc tim phèo""".split()
for w in extra: c[w]+=5
json.dump(dict(c),open('menu_vocab.json','w',encoding='utf-8'),ensure_ascii=False)
print(len(c))
for w in "gia vị dương châu tủy tiềm thuốc bắc tía tô lá ống nhúng mẻ phay riêng tươi chén đĩa uối nhím quẹt ngũ".split(): c[w]+=5
json.dump(dict(c),open('menu_vocab.json','w',encoding='utf-8'),ensure_ascii=False)
