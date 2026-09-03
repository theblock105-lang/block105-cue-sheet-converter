import csv,io,os,re
from flask import Flask,render_template,request,send_file,flash,redirect,url_for
from pypdf import PdfReader
app=Flask(__name__); app.secret_key=os.environ.get("SECRET_KEY","block105")
def clean(v): return str(v or "").strip()
def ts(v):
 v=clean(v).replace(",",".")
 if not v:return ""
 try:
  p=v.split(":")
  if len(p)==3:h,m,s=p
  elif len(p)==2:h,m,s=0,p[0],p[1]
  else:h,m,s=0,0,p[0]
  n=int(round((int(h)*3600+int(m)*60+float(s))*1000));h=n//3600000;n%=3600000;m=n//60000;n%=60000;s=n//1000;ms=n%1000
  return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
 except:return v
@app.route("/",methods=["GET","POST"])
def index():
 if request.method=="POST":
  f=request.files.get("pdf")
  if not f or not f.filename.lower().endswith(".pdf"):
   flash("Please select a completed BLOCK 105 cue sheet PDF.");return redirect(url_for("index"))
  try:
   fields=PdfReader(f).get_fields() or {}
   d={k:clean(v.get("/V","")) for k,v in fields.items()}
  except Exception as e:
   flash(f"Could not read this PDF: {e}");return redirect(url_for("index"))
  rows=[]
  for i in range(1,26):
   a=d.get(f"row_{i}_timestamp","");b=d.get(f"row_{i}_segment","");c=d.get(f"row_{i}_artist","");e=d.get(f"row_{i}_album","")
   if any([a,b,c,e]): rows.append([ts(a),"music" if e else "talk",b,c,e,""])
  if not rows:
   flash("No completed cue-sheet rows were found.");return redirect(url_for("index"))
  out=io.StringIO(newline="");w=csv.writer(out);w.writerow(["offset","media_type","title","artist","album","year"]);w.writerows(rows)
  name=re.sub(r"[^A-Za-z0-9_-]+","_",d.get("show_title") or "BLOCK_105_SHOW").strip("_")
  data=io.BytesIO(out.getvalue().encode("utf-8-sig"));data.seek(0)
  return send_file(data,mimetype="text/csv",as_attachment=True,download_name=f"{name}_LIVE365.csv")
 return render_template("index.html")
if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.environ.get("PORT",8080)))
