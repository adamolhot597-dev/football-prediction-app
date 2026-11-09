import streamlit as st
import pandas as pd
import requests
import pypdf # قراءة ملفات الـ PDF الحقيقية

# إعدادات الواجهة
st.set_page_config(
    page_title="منصة تحليل وتعلم كوطات MDJS",
    page_icon="⚽",
    layout="wide"
)

# دعم الاتجاه العربي
st.markdown("""
<style>
    body, div, span, p, table, th, td { direction: rtl; text-align: right; }
    .stDataFrame { direction: rtl; }
</style>
""", unsafe_allow_html=True)

st.title("⚽ محرك الهندسة العكسية والتعلم الذكي (MDJS)")
st.markdown("---")

st.sidebar.header("🎯 قائمة التحكم")
app_mode = st.sidebar.radio(
    "اختر القسم:",
    [
        "📥 رفع الأرشيف والتعلم التلقائي", 
        "🧠 فحص البرنامج الجديد واكتشاف النمط",
        "⚙️ ربط API النتائج (API-Football)"
    ]
)

# دالة قراءة واستخراج النصوص من ملف PDF المرفوع
def extract_matches_from_pdf(pdf_file):
    reader = pypdf.PdfReader(pdf_file)
    extracted_text = ""
    for page in reader.pages:
        extracted_text += page.extract_text() or ""
    
    # تفكيك الأسطر واستخراج المباريات المقروءة فعلياً
    lines = [line.strip() for line in extracted_text.split('\n') if line.strip()]
    
    # تجميع البيانات المقروءة في قائمة
    parsed_matches = []
    for line in lines:
        # البحث عن الأسطر التي تحتوي على أسماء الفرق أو الكوطات
        if " - " in line or "v" in line or "VS" in line:
            parsed_matches.append({
                "المباراة المقروءة من الـ PDF": line,
                "حالة الفحص": "🔍 تم استخراجها من البروغرام المرفوع"
            })
            
    return parsed_matches, extracted_text

# -------------------------------------------------------------------
# القسم الأول: رفع الأرشيف والتعلم التلقائي
# -------------------------------------------------------------------
if app_mode == "📥 رفع الأرشيف والتعلم التلقائي":
    st.subheader("📁 قسم تدريب النظام على البرامج السابقة (Auto-Backtesting)")
    st.info("قم برفع ملف البرنامج القديم (PDF) ليتم قراءته واستخراج مبارياته حقيقياً.")

    archive_pdf = st.file_uploader("📂 ارفع برنامج الرهانات القديم (PDF):", type=["pdf"], key="arch_pdf")

    if archive_pdf is not None:
        st.success(f"✅ تم استقبال البرنامج القديم: `{archive_pdf.name}`")
        if st.button("🧠 ابدأ استخراج وقراءة المباريات من الـ PDF", type="primary"):
            matches, raw_text = extract_matches_from_pdf(archive_pdf)
            if matches:
                st.write("### 📋 المباريات المستخرجة فعلياً من ملفك:")
                st.dataframe(pd.DataFrame(matches), use_container_width=True)
            else:
                st.warning("تم قراءة الملف، ولكن يتعذر قراءة الأسطر كـ نص صافٍ. جاري معالجة الهيكلة...")

# -------------------------------------------------------------------
# القسم الثاني: فحص البرنامج الجديد واكتشاف النمط
# -------------------------------------------------------------------
elif app_mode == "🧠 فحص البرنامج الجديد واكتشاف النمط":
    st.subheader("🧠 محرك الفحص والبحث عن الثغرات (Pattern Detection)")
    st.info("ارفع البرنامج الجديد (PDF) ليقوم النظام بقراءة مبارياته الحقيقية بدلاً من عرض أمثلة قديمة.")

    new_pdf = st.file_uploader("📂 ارفع برنامج الرهانات الجديد (PDF):", type=["pdf"], key="new_pdf")

    if new_pdf is not None:
        st.success(f"📄 تم استلام البروغرام الجديد: `{new_pdf.name}`")
        if st.button("🚀 تحليل المباريات المستخرجة من هاد البروغرام", type="primary"):
            st.warning("⏳ جاري تفكيك ملف الـ PDF وقراءة مبارياته الحقيقية...")
            
            # قراءة المباريات الحقيقية من الـ PDF المرفوع كلياً
            matches, raw_text = extract_matches_from_pdf(new_pdf)
            
            if matches:
                st.markdown("### 🎯 جدول المباريات المستخرجة من برامجك المرفوع:")
                st.dataframe(pd.DataFrame(matches), use_container_width=True)
            else:
                st.info("📄 النص المستخرج من البروغرام المرفوع:")
                st.text_area("محتوى الملف المقروء:", raw_text[:2000], height=250)

# -------------------------------------------------------------------
# القسم الثالث: ربط API النتائج
# -------------------------------------------------------------------
elif app_mode == "⚙️ ربط API النتائج (API-Football)":
    st.subheader("⚙️ إعدادات ربط النتائج الحية والتاريخية")
    api_key_input = st.text_input("مفتاح API-Key الخاص بك:", type="password")

    if st.button("🔗 فحص الاتصال", type="primary"):
        if api_key_input:
            url = "https://v3.football.api-sports.io/timezone"
            headers = {'x-apisports-key': api_key_input}
            try:
                response = requests.get(url, headers=headers, timeout=5)
                if response.status_code == 200:
                    st.success("✅ الاتصال مستقر وصحيح 100% مع API-Football!")
                else:
                    st.error("❌ فشل الاتصال، تأكد من المفتاح.")
            except Exception as e:
                st.error(f"❌ خطأ في الاتصال: {e}")
