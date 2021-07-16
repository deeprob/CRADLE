import io
import linecache
import marshal
import math
import multiprocessing
import os
import os.path
import tempfile
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pyBigWig
import statsmodels.api as sm

from shutil import copyfile

from CRADLE.correctbiasutils.cython import arraySplit, coalesceSections

matplotlib.use('Agg')

TRAINING_BIN_SIZE = 1_000
SCATTERPLOT_SAMPLE_COUNT = 10_000
SONICATION_SHEAR_BIAS_OFFSET = 2

# Used to adjust coordinates between 0 and 1-based systems.
START_INDEX_ADJUSTMENT = 1

class ChromoRegion:
	def __init__(self, chromo, start, end):
		self.chromo = chromo
		self.start = int(start)
		self.end = int(end)
		self._length = self.end - self.start

	def __len__(self):
		return self._length

	def __eq__(self, o: object) -> bool:
		return (self.chromo == o.chromo
			and self.start == o.start
			and self.end == o.end
			and self._length == o._length)

	def __repr__(self):
		return f"({self.chromo}:{self.start}-{self.end})"

class ChromoRegionSet:
	def __init__(self, trainingRegions=None):
		self.trainingRegions = []
		self.chromos = set()
		self.cumulativeRegionSize = 0
		if trainingRegions is not None:
			self.trainingRegions = trainingRegions
			for region in self.trainingRegions:
				self.chromos.add(region.chromo)
				self.cumulativeRegionSize += len(region)

	def __add__(self, o):
		newTrainingSet = ChromoRegionSet()
		newTrainingSet.trainingRegions = self.trainingRegions + o.trainingRegions
		newTrainingSet.cumulativeRegionSize = self.cumulativeRegionSize + o.cumulativeRegionSize
		newTrainingSet.chromos = self.chromos.union(o.chromos)

		return newTrainingSet

	def __sub__(self, o):
		pass

	def addRegion(self, region):
		self.trainingRegions.append(region)
		self.chromos.add(region.chromo)
		self.cumulativeRegionSize += len(region)

	def __len__(self) -> int:
		return len(self.trainingRegions)

	def __iter__(self):
		def regionGenerator():
			for region in self.trainingRegions:
				yield region

		return regionGenerator()

	def __eq__(self, o):
		if len(self.trainingRegions) != len(o.trainingRegions):
			return False

		for selfRegion, oRegion in zip(self.trainingRegions, o.trainingRegions):
			if selfRegion != oRegion:
				return False

		return self.cumulativeRegionSize == o.cumulativeRegionSize

	def __repr__(self):
		return f"[{', '.join([str(region) for region in self.trainingRegions])}]"

def process(poolSize, function, argumentLists):
	pool = multiprocessing.Pool(poolSize)
	results = pool.starmap_async(function, argumentLists).get()
	pool.close()
	pool.join()

	return results

def getResultBWHeader(regions, ctrlBWName):
	with pyBigWig.open(ctrlBWName) as bwFile:
		resultBWHeader = []
		for chromo in regions.chromos:
			chromoSize = bwFile.chroms(chromo)
			resultBWHeader.append( (chromo, chromoSize) )

	return resultBWHeader

def marshalFile(outputDir, data):
	with tempfile.NamedTemporaryFile(mode="wb", suffix=".msl", dir=outputDir, delete=False) as outputFile:
		name = outputFile.name
		marshal.dump(data, outputFile)

	return name

def rotateBWFileArrays(tempFiles, ctrlBWNames, experiBWNames):
	# The tempFiles list isn't in a useful shape. It's a list of pairs of lists of files
	# Example:
	# [
	#   # Results of Job 0
	#	[['ctrlFile0Temp0.msl', 'ctrlFile1Temp0.msl'], ['experiFile0Temp0.msl', 'experiFile1Temp0.msl'], ],
	#   # Results of Job 1
	#	[['ctrlFile0Temp1.msl', 'ctrlFile1Temp1.msl'], ['experiFile0Temp1.msl', 'experiFile1Temp1.msl'], ],
	#   # Results of Job 2
	#	[['ctrlFile0Temp2.msl', 'ctrlFile1Temp2.msl'], ['experiFile0Temp2.msl', 'experiFile1Temp2.msl'], ],
	# ]
	#
	# The following code rearranges the file names so they are in this shape:
	# [
	#	[
	# 		['ctrlFile0Temp0.msl', 'ctrlFile0Temp1.msl', 'ctrlFile0Temp2.msl'],
	# 		['ctrlFile1Temp0.msl', 'ctrlFile1Temp1.msl', 'ctrlFile1Temp2.msl']
	# 	],
	#	[
	# 		['experiFile0Temp0.msl', 'experiFile0Temp1.msl', 'experiFile0Temp2.msl'],
	# 		['experiFile1Temp0.msl', 'experiFile1Temp1.msl', 'experiFile1Temp2.msl']
	# 	],
	# ]
	#
	# Which matches what we want to do with the files better -- combine all temp files for a particular file together
	# into a single BigWig file

	ctrlFiles = [[] for _ in range(len(ctrlBWNames))]
	experiFiles = [[] for _ in range(len(experiBWNames))]
	for jobFiles in tempFiles:
		jobCtrl = jobFiles[0]
		jobExperi = jobFiles[1]
		for i, ctrlFile in enumerate(jobCtrl):
			ctrlFiles[i].append(ctrlFile)
		for i, experiFile in enumerate(jobExperi):
			experiFiles[i].append(experiFile)

	return ctrlFiles, experiFiles

def mergeBWFiles(outputDir, header, tempFiles, ctrlBWNames, experiBWNames):
	ctrlFiles, experiFiles = rotateBWFileArrays(tempFiles, ctrlBWNames, experiBWNames)

	jobList = []
	for i, ctrlFile in enumerate(ctrlBWNames):
		jobList.append([ctrlFiles[i], header, outputBWFile(outputDir, ctrlFile)])
	for i, experiFile in enumerate(experiBWNames):
		jobList.append([experiFiles[i], header, outputBWFile(outputDir, experiFile)])

	return process(len(ctrlBWNames) + len(experiBWNames), mergeCorrectedFilesToBW, jobList)

def outputBWFile(outputDir, filename):
	signalBWName = '.'.join(filename.rsplit('/', 1)[-1].split(".")[:-1])
	return os.path.join(outputDir, signalBWName + "_corrected.bw")

def mergeCorrectedFilesToBW(tempFiles, bwHeader, signalBWName):
	signalBW = pyBigWig.open(signalBWName, "w")
	signalBW.addHeader(bwHeader)

	for tempFile in tempFiles:
		with open(tempFile, 'rb') as dataFile:
			correctedReadCounts = marshal.load(dataFile)

		for counts in correctedReadCounts:
			chromo, readCounts = counts
			if readCounts is None:
				continue

			sectionCount, starts, ends, values = readCounts
			signalBW.addEntries([chromo] * sectionCount, starts, ends=ends, values=values)

		os.remove(tempFile)

	signalBW.close()

	return signalBWName

def divideGenome(regions, baseBinSize=1, genomeBinSize=50000):

	# Adjust the genome bin size to be a multiple of base bin Size
	genomeBinSize -= genomeBinSize % baseBinSize

	# Return an list of tuples of values instead of a ChromoRegionSet because this will be used in Cython code.
	# And the less "python" interaction Cython has, the better. Presumedly python tuples are lighter weight
	# than regular objects.
	newRegions = []
	for region in regions:
		binStart = region.start
		while binStart < region.end:
			binEnd = binStart + genomeBinSize
			newRegions.append((region.chromo, binStart, min(binEnd, region.end)))
			binStart = binEnd

	return newRegions

def genNormalizedObBWs(outputDir, header, regions, ctrlBWNames, ctrlScaler, experiBWNames, experiScaler):
	# copy the first replicate
	observedBWName = ctrlBWNames[0]
	copyfile(observedBWName, outputNormalizedBWFile(outputDir, observedBWName))

	jobList = []
	for i, ctrlBWName in enumerate(ctrlBWNames[1:], start=1):
		jobList.append([header, float(ctrlScaler[i]), regions, ctrlBWName, outputNormalizedBWFile(outputDir, ctrlBWName)])
	for i, experiBWName in enumerate(experiBWNames):
		jobList.append([header, float(experiScaler[i]), regions, experiBWName, outputNormalizedBWFile(outputDir, experiBWName)])


	return process(len(ctrlBWNames) + len(experiBWNames) - 1, generateNormalizedObBWs, jobList)

def outputNormalizedBWFile(outputDir, filename):
	normObBWName = '.'.join(filename.rsplit('/', 1)[-1].split(".")[:-1])
	return os.path.join(outputDir, normObBWName + "_normalized.bw")

def generateNormalizedObBWs(bwHeader, scaler, regions, observedBWName, normObBWName):
	with pyBigWig.open(observedBWName) as obBW, pyBigWig.open(normObBWName, "w") as normObBW:
		_generateNormalizedObBWs(bwHeader, scaler, regions, obBW, normObBW)
	return normObBWName

def _generateNormalizedObBWs(bwHeader, scaler, regions, observedBW, normObBW):
	normObBW.addHeader(bwHeader)

	for region in regions:
		starts = np.arange(region.start, region.end)
		if pyBigWig.numpy == 1:
			values = observedBW.values(region.chromo, region.start, region.end, numpy=True)
		else:
			values = np.array(observedBW.values(region.chromo, region.start, region.end))

		idx = np.where( (np.isnan(values) == False) & (values > 0))[0]
		starts = starts[idx]

		if len(starts) == 0:
			continue

		values = values[idx]
		values = values / scaler

		coalescedSectionCount, startEntries, endEntries, valueEntries = coalesceSections(starts, values)

		normObBW.addEntries([region.chromo] * coalescedSectionCount, startEntries, ends=endEntries, values=valueEntries)

def getReadCounts(trainingSet, fileName):
	values = []

	with pyBigWig.open(fileName) as bwFile:
		for region in trainingSet:
			temp = np.array(bwFile.values(region.chromo, region.start, region.end))
			idx = np.where(np.isnan(temp) == True)
			temp[idx] = 0
			temp = temp.tolist()
			values.extend(temp)

	return values

def getScalerTasks(trainingSet, observedReadCounts, ctrlBWNames, experiBWNames):
	tasks = []
	ctrlBWCount = len(ctrlBWNames)
	sampleSetCount = ctrlBWCount + len(experiBWNames)
	for i in range(1, sampleSetCount):
		if i < ctrlBWCount:
			bwName = ctrlBWNames[i]
		else:
			bwName = experiBWNames[i - ctrlBWCount]
		tasks.append([trainingSet, observedReadCounts, bwName])

	return tasks

def getScalerForEachSample(trainingSet, observedReadCounts1Values, bwFileName):
	observedReadCounts2Values = getReadCounts(trainingSet, bwFileName)
	model = sm.OLS(observedReadCounts2Values, observedReadCounts1Values).fit()
	scaler = model.params[0]

	return scaler

def selectTrainingSetFromMeta(trainingSetMetas, rc99Percentile):
	trainSet1 = ChromoRegionSet()
	trainSet2 = ChromoRegionSet()

	### trainSet1
	for binIdx in range(5):
		if trainingSetMetas[binIdx] is None:
			continue

		regionNum = int(trainingSetMetas[binIdx][2])
		candiRegionNum = int(trainingSetMetas[binIdx][3])
		candiRegionFile = trainingSetMetas[binIdx][4]

		if candiRegionNum < regionNum:
			subfileStream = open(candiRegionFile)
			subfileLines = subfileStream.readlines()

			for line in subfileLines:
				temp = line.split()
				trainSet1.addRegion(ChromoRegion(temp[0], int(temp[1]), int(temp[2])))
		else:
			selectRegionIdx = np.random.choice(list(range(candiRegionNum)), regionNum, replace=False)

			for idx in selectRegionIdx:
				temp = linecache.getline(candiRegionFile, idx+1).split()
				trainSet1.addRegion(ChromoRegion(temp[0], int(temp[1]), int(temp[2])))
		os.remove(candiRegionFile)

	### trainSet2
	for binIdx in range(5, len(trainingSetMetas)):
		if trainingSetMetas[binIdx] is None:
			continue

		# downLimit = int(trainingSetMetas[binIdx][0])
		regionNum = int(trainingSetMetas[binIdx][2])
		candiRegionNum = int(trainingSetMetas[binIdx][3])
		candiRegionFile = trainingSetMetas[binIdx][4]

		# if downLimit == rc99Percentile:
		# 	subfileStream = open(candiRegionFile)
		# 	subfile = subfileStream.readlines()

		# 	i = len(subfile) - 1
		# 	while regionNum > 0 and i >= 0:
		# 		temp = subfile[i].split()
		# 		trainSet2.addRegion(ChromoRegion(temp[0], int(temp[1]), int(temp[2])))
		# 		i = i - 1
		# 		regionNum = regionNum - 1

		# else:

		if candiRegionNum < regionNum:
			subfileStream = open(candiRegionFile)
			subfileLines = subfileStream.readlines()

			for line in subfileLines:
				temp = line.split()
				trainSet2.addRegion(ChromoRegion(temp[0], int(temp[1]), int(temp[2])))
		else:
			selectRegionIdx = np.random.choice(list(range(candiRegionNum)), regionNum, replace=False)

			for idx in selectRegionIdx:
				temp = linecache.getline(candiRegionFile, idx+1).split()
				trainSet2.addRegion(ChromoRegion(temp[0], int(temp[1]), int(temp[2])))

		os.remove(candiRegionFile)

	return trainSet1, trainSet2

def regionMeans(bwFile, binCount, chromo, start, end):
	if pyBigWig.numpy == 1:
		values = bwFile.values(chromo, start, end, numpy=True)
	else:
		values = np.array(bwFile.values(chromo, start, end))

	if binCount == 1:
		means = [np.mean(values)]
	else:
		binnedValues = arraySplit(values, binCount, fillValue=np.nan)
		means = [np.mean(x) for x in binnedValues]

	return means

def getCandidateTrainingSet(rcPercentile, regions, ctrlBWName, outputDir):
	trainRegionNum = math.pow(10, 6) / float(TRAINING_BIN_SIZE)

	meanRC = []
	totalBinNum = 0
	with pyBigWig.open(ctrlBWName) as ctrlBW:
		for region in regions:
			numBin = max(1, len(region) // TRAINING_BIN_SIZE)
			totalBinNum += numBin

			means = regionMeans(ctrlBW, numBin, region.chromo, region.start, region.end)

			meanRC.extend(means)

	if totalBinNum < trainRegionNum:
		trainRegionNum = totalBinNum

	meanRC = np.array(meanRC)
	meanRC = meanRC[np.where((np.isnan(meanRC) == False) & (meanRC > 0))]

	trainingRegionNum1 = int(np.round(trainRegionNum * 0.5 / 5))
	trainingRegionNum2 = int(np.round(trainRegionNum * 0.5 / 9))
	trainingSetMeta = []
	rc90Percentile = None
	rc99Percentile = None

	for i in range(5):
		rc1 = int(np.percentile(meanRC, int(rcPercentile[i])))
		rc2 = int(np.percentile(meanRC, int(rcPercentile[i+1])))
		temp = [rc1, rc2, trainingRegionNum1, regions, ctrlBWName, outputDir, rc90Percentile, rc99Percentile]
		trainingSetMeta.append(temp)  ## RC criteria1(down), RC criteria2(up), # of bases, candidate regions

	for i in range(5, 11):
		rc1 = int(np.percentile(meanRC, int(rcPercentile[i])))
		rc2 = int(np.percentile(meanRC, int(rcPercentile[i+1])))
		if i == 10:
			temp = [rc1, rc2, 3*trainingRegionNum2, regions, ctrlBWName, outputDir, rc90Percentile, rc99Percentile]
			rc99Percentile = rc1
		else:
			temp = [rc1, rc2, trainingRegionNum2, regions, ctrlBWName, outputDir, rc90Percentile, rc99Percentile] # RC criteria1(down), RC criteria2(up), # of bases, candidate regions
		if i == 5:
			rc90Percentile = rc1

		trainingSetMeta.append(temp)

	return trainingSetMeta, rc90Percentile, rc99Percentile

def fillTrainingSetMeta(downLimit, upLimit, trainingRegionNum, regions, ctrlBWName, outputDir, rc90Percentile = None, rc99Percentile = None):
	ctrlBW = pyBigWig.open(ctrlBWName)

	resultLine = [downLimit, upLimit, trainingRegionNum]
	numOfCandiRegion = 0

	resultFile = tempfile.NamedTemporaryFile(mode="w+t", suffix=".txt", dir=outputDir, delete=False)
	'''
	if downLimit == rc99Percentile:
		result = []

		for region in regions:
			regionChromo = region[0]
			regionStart = int(region[1])
			regionEnd = int(region[2])

			numBin = int( (regionEnd - regionStart) / TRAINING_BIN_SIZE )
			if numBin == 0:
				numBin = 1
				meanValue = np.array(ctrlBW.stats(regionChromo, regionStart, regionEnd, nBins=numBin, type="mean"))[0]

				if meanValue is None:
					continue

				if (meanValue >= downLimit) and (meanValue < upLimit):
					result.append([regionChromo, regionStart, regionEnd, meanValue])

			else:
				regionEnd = numBin * TRAINING_BIN_SIZE + regionStart
				meanValues = np.array(ctrlBW.stats(regionChromo, regionStart, regionEnd, nBins=numBin, type="mean"))
				pos = np.array(list(range(0, numBin))) * TRAINING_BIN_SIZE + regionStart

				idx = np.where(meanValues != None)
				meanValues = meanValues[idx]
				pos = pos[idx]

				idx = np.where((meanValues >= downLimit) & (meanValues < upLimit))
				start = pos[idx]
				end = start + TRAINING_BIN_SIZE
				meanValues = meanValues[idx]
				chromoArray = [regionChromo] * len(start)
				result.extend(np.column_stack((chromoArray, start, end, meanValues)).tolist())

		if len(result) == 0:
			return None

		result = np.array(result)
		result = result[result[:,3].astype(float).astype(int).argsort()][:,0:3].tolist()

		numOfCandiRegion = len(result)
		for i in range(len(result)):
			resultFile.write('\t'.join([str(x) for x in result[i]]) + "\n")
	'''
	fileBuffer = io.StringIO()
	for region in regions:
		numBin = len(region) // TRAINING_BIN_SIZE
		if numBin == 0:
			numBin = 1

			meanValue = regionMeans(ctrlBW, numBin, region.chromo, region.start, region.end)[0]

			if np.isnan(meanValue) or meanValue == 0:
				continue

			if (meanValue >= downLimit) and (meanValue < upLimit):
				fileBuffer.write(f"{region.chromo}\t{region.start}\t{region.end}\n")
				numOfCandiRegion += 1
		else:
			regionEnd = numBin * TRAINING_BIN_SIZE + region.start

			meanValues = np.array(regionMeans(ctrlBW, numBin, region.chromo, region.start, regionEnd))

			pos = np.arange(0, numBin) * TRAINING_BIN_SIZE + region.start

			idx = np.where((meanValues != None) & (meanValues > 0))
			meanValues = meanValues[idx]
			pos = pos[idx]

			idx = np.where((meanValues >= downLimit) & (meanValues < upLimit))

			if len(idx[0]) == 0:
				continue

			starts = pos[idx]

			numOfCandiRegion += len(starts)
			for start in starts:
				fileBuffer.write(f"{region.chromo}\t{start}\t{start + TRAINING_BIN_SIZE}\n")

	resultFile.write(fileBuffer.getvalue())
	ctrlBW.close()
	resultFile.close()
	fileBuffer.close()

	if numOfCandiRegion != 0:
		resultLine.extend([numOfCandiRegion, resultFile.name])
		return resultLine

	os.remove(resultFile.name)
	return None

def alignCoordinatesToCovariateFileBoundaries(genome, trainingSet, fragLen):
	newTrainingSet = ChromoRegionSet()

	for trainingRegion in trainingSet:
		chromoEnd = genome.chroms(trainingRegion.chromo)
		analysisStart = trainingRegion.start
		analysisEnd = trainingRegion.end

		# Define a region of fragments of length fragLen
		fragRegionStart = analysisStart - fragLen + START_INDEX_ADJUSTMENT
		fragRegionEnd = analysisEnd + fragLen - START_INDEX_ADJUSTMENT

		# Define a region that includes base pairs used to model shearing/sonication bias
		shearStart = fragRegionStart - SONICATION_SHEAR_BIAS_OFFSET
		shearEnd = fragRegionEnd + SONICATION_SHEAR_BIAS_OFFSET

		# Make sure the analysisStart and analysisEnd fall within the boundaries of the region
		# covariates have been precomputed for
		if shearStart < 1:
			fragRegionStart = SONICATION_SHEAR_BIAS_OFFSET + START_INDEX_ADJUSTMENT
			analysisStart = max(analysisStart, fragRegionStart)

		if shearEnd > chromoEnd:
			fragRegionEnd = chromoEnd - SONICATION_SHEAR_BIAS_OFFSET
			analysisEnd = min(analysisEnd, fragRegionEnd)

		newTrainingSet.addRegion(ChromoRegion(trainingRegion.chromo, analysisStart, analysisEnd))

	return newTrainingSet

def getScatterplotSampleIndices(populationSize):
	if populationSize <= SCATTERPLOT_SAMPLE_COUNT:
		return np.arange(0, populationSize)
	else:
		return np.random.choice(np.arange(0, populationSize), SCATTERPLOT_SAMPLE_COUNT, replace=False)

def figureFileName(outputDir, bwFilename):
	bwName = '.'.join(bwFilename.rsplit('/', 1)[-1].split(".")[:-1])
	return os.path.join(outputDir, f"fit_{bwName}.png")

def plot(regRCs, regRCFittedValues, highRCs, highRCFittedValues, figName):
	fig, ax = plt.subplots()

	corr = np.corrcoef(regRCFittedValues, regRCs)[0, 1]
	corr = np.round(corr, 2)
	maxi1 = np.nanmax(regRCFittedValues)
	maxi2 = np.nanmax(regRCs)
	maxiRegRC = max(maxi1, maxi2)
	ax.plot(regRCs, regRCFittedValues, color='g', marker='s', alpha=0.01)

	corr = np.corrcoef(highRCFittedValues, highRCs)[0, 1]
	corr = np.round(corr, 2)
	maxi1 = np.nanmax(highRCFittedValues)
	maxi2 = np.nanmax(highRCs)
	maxiHighRC = max(maxi1, maxi2)
	ax.plot(highRCs, highRCFittedValues, color='g', marker='s', alpha=0.01)

	maxi = max(maxiRegRC, maxiHighRC)

	ax.text((maxi-25), 10, corr, ha='center', va='center')
	ax.set_xlabel("observed")
	ax.set_ylabel("predicted")
	ax.set_xlim(0, maxi)
	ax.set_ylim(0, maxi)
	ax.plot([0, maxi], [0, maxi], 'k-', color='r')
	ax.set_aspect('equal', adjustable='box')
	fig.savefig(figName)
