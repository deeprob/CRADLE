#!/data/reddylab/software/miniconda2/envs/YoungSook/bin/python2.7

from distutils.core import setup, Extension
#from Cython.Build import cythonize


def main():

	'''
	setup(
  	name = 'CalculateOnebp Part',
  	ext_modules = cythonize("calculateOnebp.pyx"),
	)
	'''
	setup(name = "CRADLE",
	      version = "1.0.0",
	      description = "Correct Read Counts and Analysis of Differently Expressed Regions",
	      author = "Young-Sook Kim",
	      author_email = "kys91240@gmail.com",
	      url = "https://github.com/Young-Sook/CRADLE",
	      packages = ['CorrectBias', 'CorrectBiasStored', 'CallPeak'], # package names
	      package_dir = {'': 'CRADLE'}, # It calls ./CRADLE/CorrectBias/__init__.py
	      scripts = ["bin/cradle"], # python scource code, intended to be started from the command line.
	      install_required = [  
		       "numpy >= 1.14.3",
		       "multiprocessing >= 0.70a1",
		       "argparse >= 1.1",
                       "py2bit >= 0.3.0",
		       "pyBigWig >= 0.3.11",
		       "statsmodels >= 0.8.0",
		       "scipy >= 1.0.1",
		       "matplotlib >= 1.5.3",
		       "h5py >= 2.6.0"
               ], 
	      ext_modules = [ Extension('CRADLE.CorrectBias.calculateOnebp', ['CRADLE/CorrectBias/calculateOnebp.c']), 
		              Extension('CRADLE.CorrectBiasStored.calculateOnebp', ['CRADLE/CorrectBiasStored/calculateOnebp.c']),
			      Extension('CRADLE.CallPeak.calculateRC', ['CRADLE/CallPeak/calculateRC.c'])
              ] ### 
	      ### If meta data is needed -> add classifier = [ 'Development status:', 'Operating system'] 
	)



if __name__ == '__main__':
	main()



