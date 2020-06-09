import haplotype_scores as hap
import pedigree_graphs as graphs
import check_files as check
import pandas as pd
import numpy as np
import statistics as stat
import sys
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis

def main():
	def init(par_file):
		par_file = [lines.strip() for lines in open(par_file).readlines()]
		run_type = [lines.split()[0] for lines in par_file[1:4] if "True" in lines][0]
		parameters = {lines.split()[0]:lines.split()[1] for lines in par_file[15:]}
		files = {lines.split()[0]:lines.split()[1] for lines in par_file[6:13]}
		pars = check.start_up(parameters,files,run_type)
		return pars,run_type

	def run_hapscores(kingf,hap_file):
		relative_list = [[lines.split()[1],lines.split()[3]] for lines in open(kingf).readlines()[1:] if float(lines.split()[6]) > 0.30]
		relative_list = [min(pair) + "_" + max(pair) for pair in relative_list]
		return hap.get_hap_score(relative_list,pars,hap_file)

	#Input if a df with IID1,IID2 and hap scores calculated; output is the same df with 4 new fields:
	#AGE1,AGE2,younger ind,older ind. If age data is available for both, younger/older is determined by age.
	#Else: determined by individual haplotype scores
	def resolve_generations(df,agef,young_lab,old_lab,out):
		def add_age(df):
			age_data = {}
			if agef != "None":
				age_data = {lines.split()[0]:int(lines.split()[1]) for lines in open(agef).readlines()}
			df["AGE1"],df["AGE2"] = df["IID1"].map(age_data),df["IID2"].map(age_data)
			df["USE_H"] = df["AGE1"].isna() | df["AGE2"].isna()
			df["USE_H"] = np.where(df["AGE1"]==df["AGE2"],True,df["USE_H"])

		def resolve_h(df):
			hap_data = df[["IID1","H1","IID2","H2"]].values.tolist()
			df[old_lab] = [{i[1]:i[0],i[3]:i[2]}[min(i[1],i[3])] for i in hap_data]
			df[young_lab] = [{i[1]:i[0],i[3]:i[2]}[max(i[1],i[3])] for i in hap_data]
			df["METHOD"] = "H"
			df["STRENGTH"] = abs(df["H1"] - df["H2"])
			return df
		def resolve_age(df):
			age_data = df[["IID1","AGE1","IID2","AGE2"]].values.tolist()
			df[old_lab] = [{i[1]:i[0],i[3]:i[2]}[max(i[1],i[3])] for i in age_data]
			df[young_lab] = [{i[1]:i[0],i[3]:i[2]}[min(i[1],i[3])] for i in age_data]
			df["METHOD"] = "AGE"
			df["STRENGTH"] = abs(df["AGE1"] - df["AGE2"])
			return df

		add_age(df)
		w_age = resolve_age(df.copy()[~df["USE_H"]])
		wo_age = resolve_h(df.copy()[df["USE_H"]])
		df = pd.concat([w_age,wo_age]).drop("USE_H",axis=1)
		return df

	def po_analysis(hap_df,agef,out):
		po = [[lines.split()[1],lines.split()[3]] for lines in open(pars["king_file"]).readlines()[1:] if lines.split()[-1] == "PO"]
		po_data = pd.DataFrame([min(pairs) + "_" + max(pairs) for pairs in po],columns=["PAIR_ID"])
		po_data = pd.merge(po_data,hap_df,on="PAIR_ID",how="left")
		po_data = resolve_generations(po_data,agef,"CHILD","PARENT",out)
		with open("%s_PO.txt" % out,"w") as outfile:
			  outfile.write(po_data.to_string(index=False))

	def infer_second(king_df,relative_df,hap_df,threshold,mhs_gap,gp_gap):
		class Data:
			def __init__(self,king,rel,hap_scores,threshold,mhs_gap,gp_gap):
				#remove outliers; main goal is to remove duplicate ind (who have >1 rel)
				degrees = ["PO","FS","2nd","3rd"]
				def remove_outliers(df):
					df = df[df["GTD"] & df["DEGREE"].isin(degrees)]
					avg_sd = {}
					for degree in degrees:
						mean = stat.mean(df[df["DEGREE"]==degree]["IBD1"])
						sd = stat.stdev(df[df["DEGREE"]==degree]["IBD1"])
						avg_sd[degree] = [mean,sd]
					df = df[df.apply(lambda x: abs(x.IBD1-avg_sd[x.DEGREE][0])/avg_sd[x.DEGREE][1] < 2,axis=1)]
					df = df.drop_duplicates(subset=["PAIR_ID"])
					df = df.drop(["IID1","IID2","IBD1","IBD2","PIHAT","KINGINF","GTD"],axis=1)
					return df

				self.putative = king[(king["IBD1"] < 0.75) & (king["IBD1"] > 0.30)]
				self.putative = pd.merge(self.putative,hap_scores,on="PAIR_ID",how="left").dropna()
				rel = remove_outliers(rel)
				self.putative = pd.merge(self.putative,rel,on="PAIR_ID",how="left")

				self.training = self.putative[self.putative["DEGREE"].isin(degrees)].copy()
				self.putative = self.putative[self.putative["DEGREE"].isna()]
				self.second = ["AV","GP","MHS","PHS"]
				self.gp_gap = gp_gap
				self.mhs_gap = mhs_gap

			def check_error(self,train_lab,lab_types,error_msg):
				for labs in lab_types:
					if train_lab.count(labs) == 0:
						log.write_errors({1:[["Not enough %s pairs to train %s classifier" % (labs,error_msg),[]]]})

			def get_training(self,df,lab_type,val_list):
				vals,labs = df[val_list].values.tolist(),df[lab_type].values.tolist()
				return vals,labs

			def find_putative(self):
				train_val,train_lab = self.get_training(self.training,"DEGREE",["IBD1","IBD2"])
				self.check_error(train_lab,["2nd","3rd"],"degree")
				classif = LinearDiscriminantAnalysis().fit(train_val,train_lab)
				self.putative["SECOND_PROB"] = self.putative.apply(lambda x: classif.predict_proba([[x.IBD1,x.IBD2]])[0][0],axis=1)
				self.putative = self.putative[self.putative["SECOND_PROB"] > threshold]

			def classify_second(self,train_df,put_df):
				train_val,train_lab = self.get_training(train_df[train_df["DEGREE"]=="2nd"],"REL",["HSR","N"])
				self.check_error(train_lab,["AV","MHS","PHS","GP"],"2nd degree")
				classif = LinearDiscriminantAnalysis().fit(train_val,train_lab)
				probs = classif.predict_proba(put_df[["HSR","N"]].values.tolist())
				for index,rel in enumerate(self.second):
					put_df[rel] = [p[index] for p in probs]
				return put_df

			def set_zero(self,col,df): #where condition is true, sets the probability to 0
				df[col] = np.where(df["CONDITION"],0,df[col])

			def set_conditional(self,df): #recomputes conditional prob
				df["SUM"] = df.loc[:,self.second].sum(axis=1)
				for rels in self.second:
					df[rels] = df[rels]/df["SUM"]

			def ages(self,df): #constrain rels with age info
				df["CONDITION"] = abs(df["AGE1"]-df["AGE2"]) > self.mhs_gap
				self.set_zero("MHS",df)
				df["CONDITION"] = abs(df["AGE1"]-df["AGE2"]) < self.gp_gap
				self.set_zero("GP",df)

			def parent(self,df): #constrain HS rels with parent data
				for i,rel in enumerate(["PHS","MHS"]):
					df["CONDITION"] = df.apply(lambda x: pedigree.ruleout_hs(x.IID1,x.IID2,i+1),axis=1)
					self.set_zero(rel,df)

			def av_error(self,h1,age1,h2,age2):
				return {age1:h1,age2:h2}[max(age1,age2)] > {age1:h1,age2:h2}[min(age1,age2)]

			def write_out(self,out,df):
				self.set_conditional(df)
				df["REL"] = df[self.second].idxmax(axis=1)
				df["PROB"] = df[self.second].max(axis=1)
				df["AV_ERROR"] = np.where(df["REL"] == "AV",df.apply(lambda x: self.av_error(x.H1,x.AGE1,x.H2,x.AGE2),axis=1),False)
				with open("%s_second.txt" % out,"w") as outfile:
					outfile.write(df.to_string(index=False,na_rep="NA",columns=["PAIR_ID","YOUNGER","OLDER","REL","PROB","HSR","N","AV","GP","MHS","PHS","AV_ERROR"]))

			def run(self):
				self.find_putative()
				self.putative = self.classify_second(self.training,self.putative)
				self.putative = resolve_generations(self.putative,pars["age_file"],"YOUNGER","OLDER",pars["out"])
				self.parent(self.putative)
				self.ages(self.putative)
				self.set_conditional(self.putative)
				self.write_out(pars["out"],self.putative)

			def validation(self,out):
				second_test = self.training[self.training["DEGREE"]=="2nd"].copy()
				second_test = self.classify_second(self.training,second_test)
				second_test["PREDICTED"] = second_test[self.second].idxmax(axis=1)
				with open("%s_training_data.txt" % out,"w") as outfile:
					outfile.write(self.training.to_string(index=False,na_rep="NA"))
				log.validation(second_test)

		data = Data(king_df,relative_df,hap_df,threshold,gp_gap,mhs_gap)
		data.run()
		data.validation(pars["out"])

	#Step 1: check files
	pars,run_type = init(sys.argv[-1])

	#Init log file
	log = check.LogFile(pars,run_type)

	#Skip hap score computation if ped only
	if run_type in ["po_only","run_all"]:
		#Step 2: compute hap scores
		hap_df = run_hapscores(pars["king_file"],pars["hap_file"])

		#Step 3: analyze PO pairs
		sys.stdout.write("Analyzing PO pairs...")
		po_analysis(hap_df,pars["age_file"],pars["out"])
		sys.stdout.write("\rAnalyzing PO pairs...done\n")

	#Quit program if po only
	if run_type == "po_only":
		log.write_log()
		sys.exit()

	#Step 4: create ped structure; resolve amb sibships, add missing parents, creates 2 df: king_df, rel_df
	sys.stdout.write("Building pedigree graphs...")
	pedigree = graphs.Pedigree()
	errors = pedigree.run_PONDEROSA(pars["king_file"],pars["fam_file"],pars["out"],pars["trust_fs"])
	log.write_errors(errors)
	relative_df = pedigree.get_rels()
	king_df = pedigree.get_king()
	log.mz_twins(pedigree.get_mz_twins())
	log.relative_pairs(relative_df)
	sys.stdout.write("\rBuilding pedigree graphs...done\n")

	#Quit program if ped only
	if run_type == "ped_only":
		log.write_log()
		sys.exit()

	#Step 5: infer second deg pairs
	sys.stdout.write("Finding and inferring 2nd degree pairs...")
	infer_second(king_df,relative_df,hap_df,float(pars["likelihood"]),int(pars["gp_gap"]),int(pars["mhs_gap"]))
	sys.stdout.write("\rFinding and inferring 2nd degree pairs...done\n")

	#Finish
	sys.stdout.write("Writing log...")
	log.write_log()
	sys.stdout.write("\rWriting log...done\n")

main()

	
