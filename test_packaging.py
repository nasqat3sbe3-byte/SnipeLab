import shutil,subprocess,sys,tempfile,unittest
from pathlib import Path
class PackagingTests(unittest.TestCase):
    def test_deployment_import_and_pages(self):
        root=Path(__file__).parent
        with tempfile.TemporaryDirectory() as folder:
            for line in (root/'Dockerfile').read_text().splitlines():
                if line.startswith('COPY '):shutil.copy2(root/line.split()[1],folder)
            code='import asyncio,main; assert main.app.title; assert "رادار الانطلاق" in asyncio.run(main.focus_page()).body.decode(); assert asyncio.run(main.focus_snapshot())["picks"]==[]'
            result=subprocess.run([sys.executable,'-c',code],cwd=folder,capture_output=True,text=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
if __name__=='__main__':unittest.main()
