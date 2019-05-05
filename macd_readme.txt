To install Ta-Lib

download ta-lib http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz

$ tar -xzf ta-lib-0.4.0-src.tar.gz
$ cd ta-lib/
$ ./configure --prefix=/usr
$ make
$ sudo make install


sudo yum install python3-devel


$ pip install TA-Lib


(macd_mm_bot) [ec2-user@ip-172-31-0-18 macd_mm_bot]$ python -c "import talib; print (talib.__ta_version__)"
Traceback (most recent call last):
  File "<string>", line 1, in <module>
  File "/home/ec2-user/macd_mm_bot/lib64/python3.7/site-packages/talib/__init__.py", line 43, in <module>
    from ._ta_lib import (
ImportError: libta_lib.so.0: cannot open shared object file: No such file or directory

To fix the above error follow this thread https://github.com/mrjbq7/ta-lib/issues/6


$ LD_LIBRARY_PATH="/usr/lib:$LD_LIBRARY_PATH" python. --> works

for permanents fix.
Using LD_LIBRARY_PATH= like that sets the environment variable just once, not for all processes. If you want a more permanent fix, you can run export LD_LIBRARY_PATH=/usr/local/lib:$LD_LIBRARY_PATH, or put the export in your .bashrc file.


