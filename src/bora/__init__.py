import warnings

__version__ = "0.1.0"
__author__ = "Sam Schott"
__url__ = "https://github.com/user/cumulus"


# suppress Python 3.9 warning from rubicon-objc
warnings.filterwarnings("ignore", module="rubicon", category=UserWarning)
